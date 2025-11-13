/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

#pragma once
#include "common/json-partial.h"
#include "utils/mico-common.h"

using json = nlohmann::ordered_json;

struct MicoRequest {
    int32_t id{0};
    int32_t priority{0};
    int32_t max_tokens{1024};
    json messages;
    json tools;
    std::vector<std::map<const unsigned char*, int32_t>> modal_prts;
    bool stop = false;
};

bool from_json_to_request(const json& j, MicoRequest& r);

int32_t stop_process(bool sucess, std::string& respone, const char** content, int32_t& is_finished,
                     LlamaSeqState& state, LlamaMicoContext* context, int32_t seq_id, bool stop_infer = true);

void apply_chat_templates(common_chat_params& formatted_chat, common_chat_templates_inputs& tmpl_inputs,
                          LlamaMicoContext* context, json messages, json tools);

bool ready_modal_bitmaps(std::vector<std::map<const unsigned char*, int32_t>>& modal_prts,
                         common_chat_templates_inputs& tmpl_inputs, LlamaMicoContext* context, LlamaSeqState& state);

bool from_input_to_token_chunks(common_chat_params& formatted_chat, std::shared_ptr<mtmd::input_chunks> chunks,
                                LlamaMicoContext* context, LlamaSeqState& state);

bool crop_by_query(std::shared_ptr<mtmd::input_chunks> chunks, int32_t current_tokens, int32_t prompt_limit,
                   LlamaMicoContext* context);

bool crop_by_tokens(std::shared_ptr<mtmd::input_chunks> chunks, int32_t current_tokens, int32_t prompt_limit,
                    LlamaMicoContext* context);

void limit_prompt_tokens(std::shared_ptr<mtmd::input_chunks> chunks, int32_t n_usage_context, LlamaSeqState& state,
                         LlamaMicoContext* context);
