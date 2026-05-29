idf_component_register(SRCS "app_main.c"
                       SRC_DIRS "."
                       INCLUDE_DIRS "."
                       REQUIRES esp_wifi nvs_flash console driver mbedtls)
target_compile_options(${COMPONENT_LIB} PRIVATE "-Wno-format")
