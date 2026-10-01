#include <stdio.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#include "esp_chip_info.h"
#include "esp_flash.h"
#include "esp_log.h"

static const char *TAG = "app_main";

void app_main(void)
{
    ESP_LOGI(TAG, "Hello from ESP32 (ESP-IDF v%s)", esp_get_idf_version());

    esp_chip_info_t chip;
    esp_chip_info(&chip);
    ESP_LOGI(TAG, "Silicon: %d core(s), revision %d", chip.cores, chip.revision);

    uint32_t flash_size = 0;
    esp_err_t err = esp_flash_get_size(NULL, &flash_size);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "esp_flash_get_size failed: %s", esp_err_to_name(err));
    } else {
        ESP_LOGI(TAG, "Flash chip size: %u MB", (unsigned)(flash_size / (1024U * 1024U)));
    }

    ESP_LOGI(TAG, "Free heap: %" PRIu32 " bytes", (uint32_t)esp_get_free_heap_size());
}