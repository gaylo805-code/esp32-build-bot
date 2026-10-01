#include <stdio.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#include "esp_chip_info.h"
#include "esp_flash.h"
#include "esp_log.h"
#include "nvs_flash.h"

static const char *TAG = "app_main";

void app_main(void)
{
    ESP_LOGI(TAG, "Hello from ESP32 (ESP-IDF v%s)", esp_get_idf_version());

    esp_chip_info_t chip;
    esp_chip_info(&chip);
    ESP_LOGI(TAG, "Silicon: %d core(s), revision %d", chip.cores, chip.revision);

    esp_flash_size_t flash_size;
    esp_flash_get_size(NULL, &flash_size);
    ESP_LOGI(TAG, "Flash: %" PRIu32 " MB", (uint32_t)flash_size / (1024 * 1024));

    ESP_LOGI(TAG, "Free heap: %" PRIu32 " bytes", (uint32_t)esp_get_free_heap_size());
}