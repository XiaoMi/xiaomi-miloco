/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

#include "mico-dialog-util.h"

/** return code **/
#define MICO_SUCCESS 0
#define MICO_ERROR -1
#define MICO_ERROR_EXCEED_MAX_CONTEXT -2

#define CHAT_CMP_ID_PREFIX "local-chatcmpl-"
#define PROMPT_PROPORTION_LIMIT 0.8

bool from_json_to_request(const json& j, MicoRequest& r) {
    std::string chat_cmpl_id = j.value("id", "local-chatcmpl-0");
    std::string prefix = CHAT_CMP_ID_PREFIX;
    if (chat_cmpl_id.substr(0, prefix.size()) == prefix) {
        r.id = std::stoi(chat_cmpl_id.substr(prefix.size()));
    }

    r.priority = j.value("priority", r.priority);
    if (j.contains("messages")) r.messages = j.at("messages");
    if (j.contains("tools")) r.tools = j.at("tools");
    if (j.contains("modal_prts")) {
        for (const auto& modal : j.at("modal_prts")) {
            std::map<const unsigned char*, int32_t> modal_map;
            for (const auto& [key, value] : modal.items()) {
                std::uintptr_t addr_value = 0;
                try {
                    addr_value = static_cast<std::uintptr_t>(std::stoull(key, nullptr, 10));
                } catch (const std::exception&) {
                    LOG_ERR("ERR: invalid address in modal_prts: %s\n", key.c_str());
                    return false;
                }
                modal_map[reinterpret_cast<const unsigned char*>(addr_value)] = value;
            }
            r.modal_prts.push_back(modal_map);
        }
    }
    r.stop = j.value("stop", false);
    return true;
}

int32_t stop_process(bool sucess, std::string& respone, const char** content, int32_t& is_finished,
                     LlamaSeqState& state, LlamaMicoContext* context, int32_t seq_id, bool stop_infer,
                     bool too_long) {  // End seq_id
    state.respone = respone;
    *content = state.respone.c_str();

    if (stop_infer) {
        is_finished = 1;
        state.is_infering.store(false);
        state.n_past.store(0);

        LlamaMemoryScheduler* ms = static_cast<LlamaMemoryScheduler*>(context->memory_scheduler);
        ms->submit_clear_mem(seq_id, -1, -1);

        context->erase_seq(seq_id);
    } else {
        is_finished = 0;
    }

    if (!sucess) {
        LOG_ERR("ERR: %s", respone.c_str());
        return MICO_ERROR;  // ERR
    }

    if (too_long) return MICO_ERROR_EXCEED_MAX_CONTEXT;  // too long

    return MICO_SUCCESS;  // success
}

void apply_chat_templates(common_chat_params& formatted_chat, common_chat_templates_inputs& tmpl_inputs,
                          LlamaMicoContext* context, json messages, json tools) {
    tmpl_inputs.messages = common_chat_msgs_parse_oaicompat(messages);
    if (!tools.is_null() && !tools.empty()) {
        tmpl_inputs.tools = common_chat_tools_parse_oaicompat(tools);
    }
    tmpl_inputs.add_generation_prompt = true;
    tmpl_inputs.use_jinja = true;  // jinja not support yet
    tmpl_inputs.enable_thinking = false;
    formatted_chat = common_chat_templates_apply(context->tmpls.get(), tmpl_inputs);
}

bool ready_modal_bitmaps(std::vector<std::map<const unsigned char*, int32_t>>& modal_prts,
                         common_chat_templates_inputs& tmpl_inputs, LlamaMicoContext* context, LlamaSeqState& state) {
    if (!modal_prts.empty()) {
        for (const auto& modal : modal_prts) {
            for (const auto& [p, len] : modal) {
                auto bitmap_ptr = mtmd_helper_bitmap_init_from_buf(context->ctx_vision.get(), p, len, 0, 0);
                if (!bitmap_ptr) {
                    return false;
                }
                state.bitmaps.entries.emplace_back(bitmap_ptr);
            }
        }
    } else {
        // Images converted from base64
        for (const auto& m : tmpl_inputs.messages) {
            for (const auto& p : m.content_parts) {
                for (const auto& img : p.images) {
                    auto bitmap_ptr = mtmd_helper_bitmap_init_from_buf(
                        context->ctx_vision.get(), reinterpret_cast<const unsigned char*>(img.c_str()), img.size(), 0,
                        0);
                    if (!bitmap_ptr) {
                        return false;
                    }
                    state.bitmaps.entries.emplace_back(bitmap_ptr);
                }
            }
        }
    }
    return true;
}

bool from_input_to_token_chunks(common_chat_params& formatted_chat, std::shared_ptr<mtmd::input_chunks> chunks,
                                LlamaMicoContext* context, LlamaSeqState& state) {
    mtmd_input_text text;
    text.text = formatted_chat.prompt.c_str();
    text.add_special = true;
    text.parse_special = true;
    auto bitmaps_c_ptr = state.bitmaps.c_ptr();
    int32_t ret =
        mtmd_tokenize(context->ctx_vision.get(), chunks->ptr.get(), &text, bitmaps_c_ptr.data(), bitmaps_c_ptr.size());
    state.bitmaps.entries.clear();
    return ret == 0;
}

bool crop_by_query(std::shared_ptr<mtmd::input_chunks> chunks, int32_t current_tokens, int32_t prompt_limit,
                   LlamaMicoContext* context) {
    LOG_INF("Attemp crop by user query\n");

    int32_t chunk_size = mtmd_input_chunks_size(chunks->ptr.get());
    auto find_lable = [&](std::vector<llama_token> source, std::vector<llama_token> lable, int32_t left_index) {
        if (left_index < 0 || left_index >= source.size()) return -1;
        for (int i = left_index; i < source.size(); i++) {
            if (source[i] == lable[0]) {
                int j = 0;
                for (j; j < lable.size(); ++j) {
                    if (source[i + j] != lable[j]) {
                        break;
                    }
                }
                if (j == lable.size()) {
                    return i;
                }
            }
        }
        return -1;
    };

    auto find_lable_chunk = [&](std::shared_ptr<mtmd::input_chunks> chunks, int32_t left_chunk_index,
                                int32_t left_token_index) {
        int32_t chunk_index = -1, chunk_token = -1, index = left_chunk_index, token_index = left_token_index;
        while (index < chunk_size) {
            auto chunk = mtmd_input_chunks_get(chunks->ptr.get(), index);
            if (mtmd_input_chunk_get_type(chunk) != MTMD_INPUT_CHUNK_TYPE_TEXT) {
                index++;
                token_index = 0;
                continue;
            }
            size_t n_tokens_chunk = mtmd_input_chunk_get_n_tokens(chunk);
            const llama_token* tokens = mtmd_input_chunk_get_tokens_text(chunk, &n_tokens_chunk);
            std::vector<llama_token> tokens_vector(tokens, tokens + n_tokens_chunk);
            int32_t find_token = find_lable(tokens_vector, context->crop_tokens_lable, token_index);
            if (find_token != -1) {
                chunk_index = index;
                chunk_token = find_token;
                break;
            }

            index++;
            token_index = 0;
        }
        return std::tuple<int32_t, int32_t>(chunk_index, chunk_token);
    };

    // find crop range
    int32_t f_chunk_index = -1, f_token_index = -1;
    int32_t s_chunk_index = -1, s_token_index = -1;
    auto lable_idx = find_lable_chunk(chunks, 0, 0);
    f_chunk_index = std::get<0>(lable_idx);
    f_token_index = std::get<1>(lable_idx);
    int32_t start_chunk_index = f_chunk_index, end_chunk_index = f_chunk_index;
    int32_t end_token_index = f_token_index, start_token_index = f_token_index;
    int32_t crop_token = 0;
    while (current_tokens > prompt_limit && f_chunk_index != -1) {
        auto idx = find_lable_chunk(chunks, f_chunk_index, f_token_index + context->crop_tokens_lable.size());
        s_chunk_index = std::get<0>(idx);
        s_token_index = std::get<1>(idx);
        if (s_chunk_index == -1) break;

        int32_t new_corp = 0;
        if (f_chunk_index == s_chunk_index) {
            new_corp += s_token_index - f_token_index;
        } else {
            auto f_chunk = mtmd_input_chunks_get(chunks->ptr.get(), f_chunk_index);
            size_t f_tokens = mtmd_input_chunk_get_n_tokens(f_chunk);
            new_corp += f_tokens - f_token_index;
            for (int i = f_chunk_index + 1; i < s_chunk_index; ++i) {
                auto chunk = mtmd_input_chunks_get(chunks->ptr.get(), i);
                size_t tokens = mtmd_input_chunk_get_n_tokens(chunk);
                new_corp += tokens;
            }
            new_corp += s_token_index;
        }
        crop_token += new_corp;
        current_tokens -= new_corp;

        f_chunk_index = s_chunk_index;
        f_token_index = s_token_index;
        end_chunk_index = f_chunk_index;
        end_token_index = f_token_index;
    }

    // last query is too long, could not crop by query
    if (current_tokens > prompt_limit) return false;

    // crop to new chunks
    mtmd_input_chunks* new_chunks = mtmd_input_chunks_init();
    int index = 0;
    for (index; index < start_chunk_index; ++index) {
        auto chunk = mtmd_input_chunks_get(chunks->ptr.get(), index);
        mtmd_input_chunk* copied_chunk = mtmd_input_chunk_copy(chunk);
        mtmd_input_chunks_add_chunk(new_chunks, copied_chunk);
    }

    auto start_chunk = mtmd_input_chunks_get(chunks->ptr.get(), index);
    size_t start_tokens_num = mtmd_input_chunk_get_n_tokens(start_chunk);
    const llama_token* start_tokens_ptr = mtmd_input_chunk_get_tokens_text(start_chunk, &start_tokens_num);
    std::vector<llama_token> start_tokens(start_tokens_ptr, start_tokens_ptr + start_token_index);
    index = end_chunk_index;
    auto end_chunk = mtmd_input_chunks_get(chunks->ptr.get(), index);
    size_t end_tokens_num = mtmd_input_chunk_get_n_tokens(end_chunk);
    const llama_token* end_tokens_ptr = mtmd_input_chunk_get_tokens_text(end_chunk, &end_tokens_num);
    std::vector<llama_token> end_tokens(end_tokens_ptr + end_token_index, end_tokens_ptr + end_tokens_num);

    if (start_chunk_index == end_chunk_index) {
        start_tokens.insert(start_tokens.end(), end_tokens.begin(), end_tokens.end());
        if (start_tokens.size() > 0) {
            mtmd_input_chunk* new_chunk = mtmd_create_text_chunk(std::move(start_tokens));
            mtmd_input_chunks_add_chunk(new_chunks, new_chunk);
        }
    } else {
        if (start_tokens.size() > 0) {
            mtmd_input_chunk* start_new_chunk = mtmd_create_text_chunk(std::move(start_tokens));
            mtmd_input_chunks_add_chunk(new_chunks, start_new_chunk);
        }
        if (end_tokens.size() > 0) {
            mtmd_input_chunk* end_new_chunk = mtmd_create_text_chunk(std::move(end_tokens));
            mtmd_input_chunks_add_chunk(new_chunks, end_new_chunk);
        }
    }

    for (++index; index < chunk_size; ++index) {
        auto chunk = mtmd_input_chunks_get(chunks->ptr.get(), index);
        mtmd_input_chunks_add_chunk(new_chunks, chunk);
    }

    LOG_INF("Crop by tokens sum %d tokens\n", crop_token);
    chunks->ptr.reset(new_chunks);
    return true;
}

bool crop_by_tokens(std::shared_ptr<mtmd::input_chunks> chunks, int32_t current_tokens, int32_t prompt_limit,
                    LlamaMicoContext* context) {
    LOG_INF("Attemp crop by tokens\n");

    int32_t chunk_size = mtmd_input_chunks_size(chunks->ptr.get());
    int32_t crop_token = 0;

    // Crop by token
    mtmd_input_chunks* new_chunks = mtmd_input_chunks_init();
    int32_t remaining_tokens = prompt_limit;
    for (int i = chunk_size - 1; i >= 0 && remaining_tokens > 0; --i) {
        auto chunk = mtmd_input_chunks_get(chunks->ptr.get(), i);
        size_t n_tokens_chunk = mtmd_input_chunk_get_n_tokens(chunk);
        if (mtmd_input_chunk_get_type(chunk) == MTMD_INPUT_CHUNK_TYPE_TEXT) {
            const llama_token* tokens = mtmd_input_chunk_get_tokens_text(chunk, &n_tokens_chunk);
            int32_t tokens_to_keep = std::min((int32_t)n_tokens_chunk, remaining_tokens);

            if (tokens_to_keep > 0) {
                std::vector<llama_token> new_tokens(tokens + n_tokens_chunk - tokens_to_keep, tokens + n_tokens_chunk);
                mtmd_input_chunk* text_chunk = mtmd_create_text_chunk(std::move(new_tokens));
                mtmd_input_chunks_insert_chunk_front(new_chunks, text_chunk);
                mtmd_input_chunk_free(text_chunk);
                remaining_tokens -= tokens_to_keep;
            }
        } else {
            if (n_tokens_chunk <= remaining_tokens) {
                mtmd_input_chunk* copied_chunk = mtmd_input_chunk_copy(chunk);
                mtmd_input_chunks_insert_chunk_front(new_chunks, copied_chunk);
                mtmd_input_chunk_free(copied_chunk);
                remaining_tokens -= n_tokens_chunk;
            } else {
                // Discard modal tokens
                break;
            }
        }
    }

    crop_token = current_tokens - (prompt_limit - remaining_tokens);
    LOG_INF("Crop by tokens sum %d tokens\n", crop_token);
    // Replace original chunks
    chunks->ptr.reset(new_chunks);
    return true;
}

void limit_prompt_tokens(std::shared_ptr<mtmd::input_chunks> chunks, int32_t n_usage_context, LlamaSeqState& state,
                         LlamaMicoContext* context) {
    float prompt_proportion = PROMPT_PROPORTION_LIMIT;
    int32_t prompt_limit = n_usage_context * prompt_proportion;
    int32_t current_tokens = 0;
    int32_t chunk_size = mtmd_input_chunks_size(chunks->ptr.get());
    for (int i = 0; i < chunk_size; ++i) {
        auto chunk = mtmd_input_chunks_get(chunks->ptr.get(), i);
        current_tokens += mtmd_input_chunk_get_n_tokens(chunk);
    }

    if (current_tokens <= prompt_limit) return;

    LOG_WRN("prompt tokens num %d > usage context size %d * %f, need to crop\n", current_tokens, n_usage_context,
            prompt_proportion);

    if (crop_by_query(chunks, current_tokens, prompt_limit, context)) return;

    crop_by_tokens(chunks, current_tokens, prompt_limit, context);
}