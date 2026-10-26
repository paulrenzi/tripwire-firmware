/* Local bench transport. Single TCP client, framed JSON + PCM16LE.
 * Audio producers never wait on networking. Gaps carry sample offsets so the
 * receiver can retain the timeline and label missing samples explicitly. */
#include "wifi_detector.h"
#include "audio_processor.h"
#include "drone_detector.h"
#include "lorawan_provision.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs.h"
#include "lwip/sockets.h"
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define PORT 8765
#define SAMPLES AUDIO_PROC_FFT_SIZE
static const char *TAG = "wifi_detector";
typedef struct {
    uint64_t offset;
    int16_t pcm[SAMPLES];
} AudioBlock;
typedef struct {
    float laeq, lzeq, dbfs;
    bool alarm;
    uint32_t uptime_ms;
    uint64_t sample_offset;
    uint32_t audio_drops, dma_overflows;
} Level;
static QueueHandle_t audio_q, level_q;
static uint64_t sample_offset;
static uint32_t audio_drops;
static uint32_t boot_id;

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && (id == WIFI_EVENT_STA_START || id == WIFI_EVENT_STA_DISCONNECTED)) {
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = data;
        char address[16];
        esp_ip4addr_ntoa(&event->ip_info.ip, address, sizeof(address));
        ESP_LOGI(TAG, "Receiver address: %s:%d", address, PORT);
    }
}

void wifi_detector_init(void)
{
    audio_q = xQueueCreate(12, sizeof(AudioBlock));
    level_q = xQueueCreate(1, sizeof(Level));
    ESP_ERROR_CHECK(audio_q && level_q ? ESP_OK : ESP_ERR_NO_MEM);
    boot_id = esp_random();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));
    char ssid[33] = {0}, pass[64] = {0};
    nvs_handle_t h;
    if (nvs_open("detector_wifi", NVS_READONLY, &h) == ESP_OK) {
        size_t n = sizeof(ssid);
        nvs_get_str(h, "wifi_ssid", ssid, &n);
        n = sizeof(pass);
        nvs_get_str(h, "wifi_pass", pass, &n);
        nvs_close(h);
    }
    bool station = ssid[0] != '\0';
    if (station) esp_netif_create_default_wifi_sta();
    else esp_netif_create_default_wifi_ap();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    wifi_config_t cfg = {0};
    if (station) {
        memcpy(cfg.sta.ssid, ssid, strlen(ssid));
        memcpy(cfg.sta.password, pass, strlen(pass));
        ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    } else {
        const char *ap = "Tripwire-XIAO";
        memcpy(cfg.ap.ssid, ap, strlen(ap));
        cfg.ap.ssid_len = strlen(ap);
        memcpy(cfg.ap.password, "tripwire-local", 14);
        cfg.ap.authmode = WIFI_AUTH_WPA2_PSK;
        cfg.ap.max_connection = 2;
        cfg.ap.channel = 1;
        ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &cfg));
    }
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    if (!station) ESP_LOGI(TAG, "Join Tripwire-XIAO; receiver address 192.168.4.1:%d", PORT);
}

/* Called from AudioTask in the opt-in Wi-Fi configuration. */
// cppcheck-suppress unusedFunction
void wifi_detector_pcm(const int32_t *samples, int count)
{
    if (count != SAMPLES) return;
    /* Static buffer is owned exclusively by AudioTask. */
    static AudioBlock block;
    block.offset = sample_offset;
    sample_offset += count;
    for (int i = 0; i < count; ++i) block.pcm[i] = (int16_t)(samples[i] >> 16);
    if (xQueueSend(audio_q, &block, 0) != pdTRUE) audio_drops++;
}

/* Called from AudioTask in the opt-in Wi-Fi configuration. */
// cppcheck-suppress unusedFunction
void wifi_detector_level(float laeq, float lzeq, float dbfs, bool alarm, uint32_t dma_overflows)
{
    Level level = { .laeq = laeq, .lzeq = lzeq, .dbfs = dbfs, .alarm = alarm,
        .uptime_ms = (uint32_t)(esp_timer_get_time() / 1000),
        .sample_offset = sample_offset, .audio_drops = audio_drops, .dma_overflows = dma_overflows };
    xQueueOverwrite(level_q, &level);
}

static bool send_all(int fd, const void *data, size_t len)
{
    const uint8_t *p = data;
    while (len) {
        int n = send(fd, p, len, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return false;
        p += n;
        len -= n;
    }
    return true;
}

static bool packet(int fd, uint8_t kind, const void *data, uint32_t len)
{
    uint8_t header[9] = {'T', 'W', '0', '1', kind,
        len & 255, (len >> 8) & 255, (len >> 16) & 255, (len >> 24) & 255};
    return send_all(fd, header, sizeof(header)) && send_all(fd, data, len);
}

void WifiDetectorTask(void *arg)
{
    (void)arg;
    int server = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (server < 0) { ESP_LOGE(TAG, "socket failed"); vTaskDelete(NULL); return; }
    int reuse = 1;
    setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_port = htons(PORT),
                                  .sin_addr.s_addr = htonl(INADDR_ANY)};
    if (bind(server, (struct sockaddr *)&address, sizeof(address)) || listen(server, 1)) {
        ESP_LOGE(TAG, "bind/listen failed: %d", errno);
        close(server); vTaskDelete(NULL); return;
    }
    /* Static network-task buffers avoid large stack frames. */
    static AudioBlock block;
    char json[512];
    for (;;) {
        int fd = accept(server, NULL, NULL);
        if (fd < 0) { vTaskDelay(pdMS_TO_TICKS(100)); continue; }
        struct timeval timeout = {.tv_sec = 2, .tv_usec = 0};
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
        xQueueReset(audio_q);
        xQueueReset(g_drone_event_queue);
        int len = snprintf(json, sizeof(json),
            "{\"type\":\"hello\",\"device_id\":%u,\"boot_id\":%" PRIu32 ","
            "\"sample_rate\":16000,\"channels\":1,\"sample_width\":2}",
            lorawan_get_keys()->device_id, boot_id);
        bool ok = packet(fd, 1, json, len);
        ESP_LOGI(TAG, "Receiver connected");
        while (ok) {
            DroneEvent_t event;
            while (ok && xQueueReceive(g_drone_event_queue, &event, 0) == pdTRUE) {
                len = snprintf(json, sizeof(json),
                    "{\"type\":\"event\",\"event\":\"%s\",\"uptime_ms\":%" PRIu32
                    ",\"confidence\":%.5f,\"rms\":%.8f,\"f0_bin\":%d}",
                    event.type == DRONE_EVENT_ALARM ? "alarm" : "clear",
                    event.timestamp_ms, event.peak_ratio, event.rms, event.f0_bin);
                ok = packet(fd, 1, json, len);
            }
            Level level;
            if (ok && xQueueReceive(level_q, &level, 0) == pdTRUE) {
                len = snprintf(json, sizeof(json),
                    "{\"type\":\"level\",\"uptime_ms\":%" PRIu32 ",\"laeq_dba\":%.2f,"
                    "\"lzeq_db\":%.2f,\"rms_dbfs\":%.2f,\"alarm\":%s,"
                    "\"sample_offset\":%" PRIu64 ",\"audio_drops\":%" PRIu32 ",\"dma_overflows\":%" PRIu32 "}",
                    level.uptime_ms, level.laeq, level.lzeq, level.dbfs,
                    level.alarm ? "true" : "false", level.sample_offset, level.audio_drops, level.dma_overflows);
                ok = packet(fd, 1, json, len);
            }
            if (ok && xQueueReceive(audio_q, &block, pdMS_TO_TICKS(20)) == pdTRUE)
                /* ESP32 is little endian. Avoid tail padding on the wire. */
                ok = packet(fd, 2, &block, 8 + sizeof(block.pcm));
        }
        ESP_LOGW(TAG, "Receiver disconnected; recording resumes on reconnect");
        close(fd);
    }
}
