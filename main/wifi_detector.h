#pragma once
#include <stdbool.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
void wifi_detector_init(void);
void WifiDetectorTask(void *arg);
void wifi_detector_pcm(const int32_t *samples, int count);
void wifi_detector_level(float laeq, float lzeq, float dbfs, bool alarm, uint32_t dma_overflows);
#ifdef __cplusplus
}
#endif
