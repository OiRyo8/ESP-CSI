/*
 * Cleaned Wi-Fi CSI data collector for ESP32
 * Optimized for HT-LTF collection and external Python script parsing.
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "esp_csi_gain_ctrl.h"

#include "esp_mac.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "nvs_flash.h"

#include "lwip/inet.h"
#include "ping/ping_sock.h"

// Подключаем встроенную библиотеку для быстрого кодирования в Base64
#include "mbedtls/base64.h"

// --- НАСТРОЙКИ WI-FI ---
// Укажите здесь ваши актуальные данные или те, что прописаны в gui_config.json
#define WIFI_SSID "Google Pixel 7 Pro"
#define WIFI_PASS "F9IRDQ006868"

#define CONFIG_LESS_INTERFERENCE_CHANNEL 11
#define CONFIG_SEND_DATA_FREQUENCY       50

static const char *TAG = "csi_collector";
static QueueHandle_t g_csi_info_queue = NULL;
static esp_ping_handle_t s_ping_handle = NULL;
static uint8_t s_ap_bssid[6] = { 0 };

// Задача для пинга роутера (стимуляция обмена пакетами для сбора CSI)
static void trigger_router_send_data_task(void *arg)
{
	if (s_ping_handle) {
		esp_ping_stop(s_ping_handle);
		esp_ping_delete_session(s_ping_handle);
		s_ping_handle = NULL;
	}

	esp_ping_config_t config = ESP_PING_DEFAULT_CONFIG();
	config.count       = 0; // Бесконечный пинг
	config.data_size   = 1;
	config.interval_ms = 1000 / CONFIG_SEND_DATA_FREQUENCY;

	esp_netif_ip_info_t local_ip;
	esp_netif_get_ip_info(esp_netif_get_handle_from_ifkey("WIFI_STA_DEF"), &local_ip);
	ESP_LOGI(TAG, "Ping target GW: " IPSTR, IP2STR(&local_ip.gw));
    
	config.target_addr.u_addr.ip4.addr = ip4_addr_get_u32(&local_ip.gw);
	config.target_addr.type = ESP_IPADDR_TYPE_V4;

	esp_ping_callbacks_t cbs = { 0 };
	esp_ping_new_session(&config, &cbs, &s_ping_handle);
	esp_ping_start(s_ping_handle);

	vTaskDelete(NULL);
}

// Обработчик событий Wi-Fi
static void wifi_event_handler(void *arg,
	esp_event_base_t event_base,
	int32_t event_id,
	void *event_data)
{
	if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
		esp_wifi_connect();
	}
	else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
		ESP_LOGW(TAG, "Wi-Fi disconnected. Reconnecting...");
		if (s_ping_handle) {
			esp_ping_stop(s_ping_handle);
		}
		esp_wifi_connect();
	}
	else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
		ESP_LOGI(TAG, "Got IP. Starting ping to trigger CSI.");
        
		// Сохраняем BSSID нашей точки доступа для фильтрации лишних CSI пакетов
		wifi_ap_record_t ap_info;
		if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK) {
			memcpy(s_ap_bssid, ap_info.bssid, 6);
		}
        
		xTaskCreate(trigger_router_send_data_task, "trigger_router_send_data", 4 * 1024, NULL, 5, NULL);
	}
}

// Callback получения сырых данных CSI
static void wifi_csi_raw_cb(void *ctx, wifi_csi_info_t *info)
{
	if (!info || !info->buf || info->len == 0) {
		return;
	}

	// Оставляем только пакеты от нашего роутера
	if (memcmp(info->mac, s_ap_bssid, 6) != 0) {
		return;
	}

	// Выделяем память под структуру и сами данные единым блоком
	wifi_csi_info_t *q_data = malloc(sizeof(wifi_csi_info_t) + info->len);
	if (!q_data) {
		return;
	}

	*q_data = *info;
	q_data->buf = (int8_t *)(q_data + 1);
	memcpy(q_data->buf, info->buf, info->len);

	if (!g_csi_info_queue || xQueueSend(g_csi_info_queue, &q_data, 0) == pdFALSE) {
		free(q_data);
	}
}

// Задача вывода данных в UART (Формат адаптирован под Phase_calc2.py)
static void csi_data_print_task(void *arg)
{
	uint8_t agc_gain = 0;
	int8_t fft_gain = 0;
    
	wifi_csi_info_t *info = NULL;
	uint32_t count = 0;

	// 1. ВЫДЕЛЯЕМ ПАМЯТЬ ОДИН РАЗ ДО ЦИКЛА
	char *buffer = malloc(8 * 1024);
	size_t max_b64_len = 2048; // С запасом для любых пакетов CSI
	char *b64_buf = malloc(max_b64_len);

	if (!buffer || !b64_buf) {
		ESP_LOGE(TAG, "Failed to allocate print buffers");
		if (buffer) free(buffer);
		if (b64_buf) free(b64_buf);
		vTaskDelete(NULL);
		return;
	}

	while (xQueueReceive(g_csi_info_queue, &info, portMAX_DELAY)) {
		size_t len = 0;
		wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;

		esp_csi_gain_ctrl_get_rx_gain(rx_ctrl, &agc_gain, &fft_gain);
        
		if (count == 0) {
			ESP_LOGI(TAG, "================ CSI RECV ================");
			len += sprintf(buffer + len, "type,seq,timestamp,taget_seq,taget,mac,rssi,rate,sig_mode,mcs,cwb,smoothing,not_sounding,aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel_primary,channel_secondary,local_timestamp,ant,sig_len,rx_state,agc_gain,fft_gain,len,first_word_invalid,data\n");
		}

		size_t out_len = 0;
		// 2. ИСПОЛЬЗУЕМ ПРЕАЛЛОЦИРОВАННЫЙ БУФЕР (нет фрагментации кучи)
		int ret = mbedtls_base64_encode((unsigned char *)b64_buf, max_b64_len, &out_len, (const unsigned char *)info->buf, info->len);
        
		if (ret != 0) {
			free(info);
			continue;
		}
		b64_buf[out_len] = '\0'; 

		len += sprintf(buffer + len,
			"CSI_DATA,%lu,%lu,0,unknown," MACSTR ",%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%lu,%d,%d,%d,%d,%d,%d,0,%s\n",
			count++,
			esp_log_timestamp(),
			MAC2STR(info->mac),
			rx_ctrl->rssi,
			rx_ctrl->rate,
			rx_ctrl->sig_mode,
			rx_ctrl->mcs,
			rx_ctrl->cwb,
			rx_ctrl->smoothing,
			rx_ctrl->not_sounding,
			rx_ctrl->aggregation,
			rx_ctrl->stbc,
			rx_ctrl->fec_coding,
			rx_ctrl->sgi,
			rx_ctrl->noise_floor,
			rx_ctrl->ampdu_cnt,
			rx_ctrl->channel,
			rx_ctrl->secondary_channel,
			rx_ctrl->timestamp,
			rx_ctrl->ant,
			rx_ctrl->sig_len,
			rx_ctrl->rx_state,
			agc_gain,
			fft_gain,
			info->len,
			b64_buf);

		printf("%s", buffer);
		free(info); // Освобождаем только структуру, пришедшую из очереди
	}

	free(buffer);
	free(b64_buf);
	vTaskDelete(NULL);
}

void app_main(void)
{
	ESP_ERROR_CHECK(nvs_flash_init());
	ESP_ERROR_CHECK(esp_netif_init());
	ESP_ERROR_CHECK(esp_event_loop_create_default());

	esp_netif_create_default_wifi_sta();
    
	wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
	ESP_ERROR_CHECK(esp_wifi_init(&cfg));

	ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL));
	ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL));

	wifi_config_t wifi_config = {
		.sta = {
		.ssid = WIFI_SSID,
		.password = WIFI_PASS,
	},
	};

	ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
	ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
	ESP_ERROR_CHECK(esp_wifi_start());
	ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

	// Настройка CSI напрямую через ESP-IDF API
	ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_raw_cb, NULL));
    
	wifi_csi_config_t csi_config = { 0 };
    
	// Включаем сбор HT-LTF
	csi_config.lltf_en        = true;
	csi_config.htltf_en       = true;
	csi_config.stbc_htltf2_en = true;
	csi_config.ltf_merge_en   = true; 
    
	ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
	ESP_ERROR_CHECK(esp_wifi_set_csi(true));

	g_csi_info_queue = xQueueCreate(64, sizeof(void *));
	xTaskCreate(csi_data_print_task, "csi_data_print", 8 * 1024, NULL, 5, NULL);
}