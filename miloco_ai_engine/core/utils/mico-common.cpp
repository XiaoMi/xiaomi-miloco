/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

#include "mico-common.h"

LlamaMicoContext::LlamaMicoContext(common_params& params) : llama_init(common_init_from_params(params)) {
    model = llama_init.model.get();
    lctx = llama_init.context.get();
    vocab = llama_model_get_vocab(model);
    smpl = common_sampler_init(model, params.sampling);
    n_threads = params.cpuparams.n_threads;
    n_batch = params.n_batch;
    n_usage_context = params.n_usage_context;

    n_seq_max = params.n_seq_max;
    n_seq_max -= params.cache_seq;  // reserved space for cache

    kv_cache_seq = params.cache_seq;

    // memory_scheduler
    memory_scheduler = new LlamaMemoryScheduler(lctx);

    if (!model || !lctx) {
        exit(1);
    }

    if (!llama_model_chat_template(model, nullptr) && params.chat_template.empty()) {
        LOG_ERR("Model does not have chat template.\n");
        LOG_ERR("  For old llava models, you may need to use '--chat-template vicuna'\n");
        LOG_ERR("  For MobileVLM models, use '--chat-template deepseek'\n");
        LOG_ERR("  For Mistral Small 3.1, use '--chat-template mistral-v7'\n");
        exit(1);
    }

    tmpls = common_chat_templates_init(model, params.chat_template);
    LOG_INF("%s: chat template example:\n%s\n", __func__,
            common_chat_format_example(tmpls.get(), params.use_jinja).c_str());

    std::string placeholder = "*=*";
    std::vector<llama_token> placeholder_tokens = common_tokenize(lctx, placeholder, false, true);
    std::string user_label = common_chat_format_example(tmpls.get(), "user", placeholder, true /*use_jinja*/);
    std::vector<llama_token> label_tokens = common_tokenize(lctx, user_label, false, true);
    // find the first occurrence of placeholder_tokens in label_tokens
    for (size_t i = 0; i <= label_tokens.size() - placeholder_tokens.size(); ++i) {
        bool found = true;
        for (size_t j = 0; j < placeholder_tokens.size(); ++j) {
            if (label_tokens[i + j] != placeholder_tokens[j]) {
                found = false;
                break;
            }
        }
        if (found) {
            crop_tokens_lable = std::vector<llama_token>(label_tokens.begin(), label_tokens.begin() + i);
            break;
        }
    }

    init_vision_context(params);

    // load antiprompt tokens for legacy templates
    if (params.chat_template == "vicuna") {
        antiprompt_tokens = common_tokenize(lctx, "ASSISTANT:", false, true);
    } else if (params.chat_template == "deepseek") {
        antiprompt_tokens = common_tokenize(lctx, "###", false, true);
    }
}

LlamaMicoContext::~LlamaMicoContext() { common_sampler_free(smpl); }

LlamaSeqState& LlamaMicoContext::get_seq_state(size_t seq_id) {
    std::lock_guard<std::mutex> lock(process_seqs_mutex);
    return process_seqs[seq_id];
}

int32_t LlamaMicoContext::set_seq_id(size_t cmpl_id) {
    std::lock_guard<std::mutex> lock(cmpl_to_seq_mutex);
    int32_t seq_id = -1;
    for (int i = 0; i < n_seq_max; i++)
        if (!get_seq_state(i).is_infering.load()) {
            seq_id = i;
            break;
        }
    if (seq_id != -1) cmpl_to_seq[cmpl_id] = seq_id;
    return seq_id;
}
int32_t LlamaMicoContext::get_seq_id(size_t cmpl_id) {
    std::lock_guard<std::mutex> lock(cmpl_to_seq_mutex);
    int32_t has = (cmpl_to_seq.count(cmpl_id) > 0 ? cmpl_to_seq[cmpl_id] : -1);
    return has;
}
bool LlamaMicoContext::erase_seq(int32_t seq_id) {
    std::lock_guard<std::mutex> lock(cmpl_to_seq_mutex);
    size_t to_erase = -1;
    for (auto& it : cmpl_to_seq) {
        if (it.second == seq_id) {
            to_erase = it.first;
            break;
        }
    }
    if (to_erase >= 0) cmpl_to_seq.erase(to_erase);
    return to_erase >= 0;
}

void LlamaMicoContext::init_vision_context(common_params& params) {
    const char* clip_path = params.mmproj.path.c_str();
    mtmd_context_params mparams = mtmd_context_params_default();
    mparams.use_gpu = params.mmproj_use_gpu;
    mparams.print_timings = true;
    mparams.n_threads = params.cpuparams.n_threads;
    mparams.verbosity = params.verbosity > 0 ? GGML_LOG_LEVEL_DEBUG : GGML_LOG_LEVEL_INFO;
    ctx_vision.reset(mtmd_init_from_file(clip_path, model, mparams));
    if (!ctx_vision.get()) {
        LOG_ERR("Failed to load vision model from %s\n", clip_path);
        exit(1);
    }
}

bool LlamaMicoContext::check_antiprompt(const llama_tokens& generated_tokens) {
    if (antiprompt_tokens.empty() || generated_tokens.size() < antiprompt_tokens.size()) {
        return false;
    }
    return std::equal(generated_tokens.end() - antiprompt_tokens.size(), generated_tokens.end(),
                      antiprompt_tokens.begin());
}