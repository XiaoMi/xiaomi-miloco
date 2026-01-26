/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */
#ifndef LLAMA_MICO_H
#define LLAMA_MICO_H

#include <stddef.h>
#include <stdint.h>

#ifdef _WIN32
#ifdef LLAMA_MICO_EXPORTS
#define LLAMA_MICO_API __declspec(dllexport)
#else
#define LLAMA_MICO_API __declspec(dllimport)
#endif
#else
#define LLAMA_MICO_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Initialize Llama Mico context
 * @param config_json JSON configuration string containing model path, multimodal projection path, etc. (currently not
 * effective)
 * @param handle Output parameter, returns the initialized context handle
 * @return 0 on success, -1 on failure
 *
 * Configuration JSON format example:
 * {
 *   "model_path": "/path/to/model.gguf",
 *   "mmproj_path": "/path/to/mmproj.gguf",
 *   "model": "model_alias",
 *   "n_gpu_layers": 50,
 *   "total_context_num": 32768,
 *   "chunk_size": 1024,
 *   "n_seq_max": 35,
 *   "cache_seq_num": 8,
 * }
 */
LLAMA_MICO_API int32_t llama_mico_init(const char *config_json, void **handle);

/**
 * @brief Free Llama Mico context
 * @param handle Context handle
 * @return 0 on success, -1 on failure
 */
LLAMA_MICO_API int32_t llama_mico_free(void *handle);

/**
 * @brief Process initial prompt request (OpenAI compatible format)
 * @param handle Context handle
 * @param request_json_str Request JSON string in OpenAI format
 * @param is_finished Output parameter, returns whether generation is finished (1 for finished, 0 to continue)
 * @param content Output parameter, returns generated content (error message if return is -1, otherwise normal text)
 * @return 0 on success, -1 on failure
 */
LLAMA_MICO_API int32_t llama_mico_request_prompt(void *handle, const char *request_json_str, int32_t *is_finished,
                                                 const char **content);

/**
 * @brief Generate next token (OpenAI compatible format)
 * @param handle Context handle
 * @param request_json_str Request JSON string in OpenAI format
 * @param is_finished Output parameter, returns whether generation is finished (1 for finished, 0 to continue)
 * @param content Output parameter, returns generated content (error message if return is -1, otherwise normal text)
 * @return 0 on success, -1 on failure
 */
LLAMA_MICO_API int32_t llama_mico_request_generate(void *handle, const char *request_json_str, int32_t *is_finished,
                                                   const char **content);

#ifdef __cplusplus
}
#endif

#endif  // LLAMA_MICO_H