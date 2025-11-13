/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

#include "llama-mico.h"

#include <cstdint>
#include <cstring>

#include "batch_scheduling/batch-scheduler.h"
#include "common/chat.h"
#include "common/json-partial.h"
#include "common/log.h"
#include "llama-cparams.h"
#include "llama.h"
#include "utils/llama-memory-scheduling.h"
#include "utils/mico-config.h"
#include "utils/mico-dialog-util.h"

using json = nlohmann::ordered_json;

#define CHAT_CMP_ID_PREFIX "local-chatcmpl-"
#define DEFAULT_ERROR_SEQ_ID -1  // NOTE: error sequence id message not thread-safe

int32_t llama_mico_init(const char* config_json, void** handle) {
    ggml_time_init();
    common_params params;
    if (!config_params_parse_json(config_json, params)) {
        LOG_ERR("ERR: prase mico config\n");
        return -1;
    }
    common_init();

    LlamaMicoContext* ctx = new LlamaMicoContext(params);
    if (!ctx || !ctx->lctx || !ctx->model) {
        LOG_ERR("ERR: failed to initialize LlamaMicoContext\n");
        delete ctx;
        return -1;
    }
    *handle = ctx;

    // BatchScheduler
    BatchScheduler* bs = new BatchScheduler(ctx, 3);
    ctx->batch_scheduler = bs;

    return 0;
}

int32_t llama_mico_free(void* handle) {
    if (!handle) {
        LOG_ERR("ERR: handle is null\n");
        return -1;
    }
    LlamaMicoContext* ctx = static_cast<LlamaMicoContext*>(handle);
    if (ctx->batch_scheduler) {
        delete static_cast<BatchScheduler*>(ctx->batch_scheduler);
        ctx->batch_scheduler = nullptr;
    }
    if (ctx->memory_scheduler) {
        delete static_cast<LlamaMemoryScheduler*>(ctx->memory_scheduler);
        ctx->memory_scheduler = nullptr;
    }
    delete ctx;
    return 0;
}

int32_t llama_mico_request_prompt(void* handle, const char* request_json_str, int32_t* is_finished,
                                  const char** content) {
    LlamaMicoContext* ctx = static_cast<LlamaMicoContext*>(handle);

    json request_json = json::parse(request_json_str);
    // printf("request_json_str: %s\n", request_json_str);
    MicoRequest request;
    if (!from_json_to_request(request_json, request)) {
        auto& err_state = ctx->get_seq_state(DEFAULT_ERROR_SEQ_ID);
        std::string err = "ERR: failed to parse request_json\n";
        return stop_process(false /* success */, err, content, *is_finished, err_state, ctx, DEFAULT_ERROR_SEQ_ID,
                            false /* stop */);
    }

    int32_t seq_id = ctx->set_seq_id(request.id);                       // Ensure seq_id is within bounds
    if (seq_id < 0 || ctx->get_seq_state(seq_id).is_infering.load()) {  // sequence request limit
        auto& err_state = ctx->get_seq_state(DEFAULT_ERROR_SEQ_ID);
        std::string err = "ERR: excessive concurrent requests\n";
        return stop_process(false /* success */, err, content, *is_finished, err_state, ctx, DEFAULT_ERROR_SEQ_ID,
                            false /* stop */);
    }

    auto& state = ctx->get_seq_state(seq_id);
    state.is_infering.store(true);

    common_chat_templates_inputs tmpl_inputs;
    common_chat_params formatted_chat;
    try {
        apply_chat_templates(formatted_chat, tmpl_inputs, ctx, request.messages, request.tools);
    } catch (const std::exception& e) {
        std::string exception(e.what());
        std::string err = "failed to parse messages, err: " + exception + "\n";
        return stop_process(false /* success */, err, content, *is_finished, state, ctx, seq_id, true /* stop */);
    }

    if (!ready_modal_bitmaps(request.modal_prts, tmpl_inputs, ctx, state)) {
        std::string err = "failed to init bitmap from buf\n";
        return stop_process(false /* success */, err, content, *is_finished, state, ctx, seq_id, true /* stop */);
    }

    auto chunks = std::make_shared<mtmd::input_chunks>(mtmd_input_chunks_init());
    if (!from_input_to_token_chunks(formatted_chat, chunks, ctx, state)) {
        std::string err = "tokenize failed, chat-cmpl-" + std::to_string(seq_id) + "\n";
        return stop_process(false /* success */, err, content, *is_finished, state, ctx, seq_id, true /* stop */);
    }

    limit_prompt_tokens(chunks, ctx->n_usage_context, state, ctx);

    /*================infer=====================*/
    BatchScheduler* bs = static_cast<BatchScheduler*>(ctx->batch_scheduler);
    bs->blocking_infer(chunks, seq_id, request.priority);
    llama_token token_id = ctx->get_seq_state(seq_id).last_token.load();

    std::string res = "";
    if (llama_vocab_is_eog(ctx->vocab, token_id) || token_id < 0) {
        return stop_process(true /* success */, res, content, *is_finished, state, ctx, seq_id, true /* stop */);
    }
    res = common_token_to_piece(ctx->lctx, token_id);
    return stop_process(true /* success */, res, content, *is_finished, state, ctx, seq_id, false /* stop */);
}

LLAMA_MICO_API int32_t llama_mico_request_generate(void* handle, const char* request_json_str, int32_t* is_finished,
                                                   const char** content) {
    LlamaMicoContext* ctx = static_cast<LlamaMicoContext*>(handle);

    json request_json = json::parse(request_json_str);
    // printf("request_json_str: %s\n", request_json_str);
    MicoRequest request;
    if (!from_json_to_request(request_json, request)) {
        auto& err_state = ctx->get_seq_state(DEFAULT_ERROR_SEQ_ID);
        std::string err = "ERR: failed to parse request_json\n";
        return stop_process(false /* success */, err, content, *is_finished, err_state, ctx, DEFAULT_ERROR_SEQ_ID,
                            false /* stop */);
    }

    int32_t seq_id = ctx->get_seq_id(request.id);                        // Ensure seq_id is within bounds
    if (seq_id < 0 || !ctx->get_seq_state(seq_id).is_infering.load()) {  // sequence request limit
        auto& err_state = ctx->get_seq_state(DEFAULT_ERROR_SEQ_ID);
        std::string err = "chat-cmpl-" + std::to_string(seq_id) + " is not in infering, please request prompt\n";
        return stop_process(false /* success */, err, content, *is_finished, err_state, ctx, DEFAULT_ERROR_SEQ_ID,
                            true /* stop */);
    }

    auto& state = ctx->get_seq_state(seq_id);
    if (request.stop) {
        std::string res = "";
        return stop_process(true /* success */, res, content, *is_finished, state, ctx, seq_id, true /* stop */);
    }
    // exceed max context
    if (state.n_past.load() >= ctx->n_usage_context) {
        std::string res = "";
        return stop_process(true /* success */, res, content, *is_finished, state, ctx, seq_id, true /* stop */,
                            true /* too long */);
    }

    llama_token last_token = state.last_token;
    std::vector<llama_token> tokens_text{last_token};
    mtmd_input_chunks* text_chunks = mtmd_create_text_chunks(tokens_text);
    auto chunks = std::make_shared<mtmd::input_chunks>(text_chunks);

    /*================infer=====================*/
    BatchScheduler* bs = static_cast<BatchScheduler*>(ctx->batch_scheduler);
    bs->blocking_infer(chunks, seq_id);

    llama_token token_id = ctx->get_seq_state(seq_id).last_token.load();
    if (token_id < 0) {
        std::string err = "chat-cmpl-" + std::to_string(seq_id) + " last token is invalid, please request prompt\n";
        return stop_process(false /* success */, err, content, *is_finished, state, ctx, seq_id, true /* stop */);
    }

    std::string res = "";
    if (llama_vocab_is_eog(ctx->vocab, token_id)) {
        return stop_process(true /* success */, res, content, *is_finished, state, ctx, seq_id, true /* stop */);
    }
    res = common_token_to_piece(ctx->lctx, token_id);
    return stop_process(true /* success */, res, content, *is_finished, state, ctx, seq_id, false /* stop */);
}