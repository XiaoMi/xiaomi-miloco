/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */
#ifndef MICO_COMMON_H
#define MICO_COMMON_H
#include <limits.h>

#include <map>
#include <mutex>

#include "common/sampling.h"
#include "mutil-modal/mtmd-helper.h"
#include "mutil-modal/mtmd.h"
#include "utils/llama-memory-scheduling.h"

struct LlamaSeqState {
    std::atomic<llama_token> last_token{-1};
    std::atomic<size_t> n_past{0};
    // int n_max_genarate{INT_MAX};
    std::atomic<bool> is_infering{false};  // true if this sequence is already inferred
    std::string respone{""};               // last text generated for this sequence
    mtmd::bitmaps bitmaps;
};

struct LlamaMicoContext {
    mtmd::context_ptr ctx_vision;   // for modal
    common_init_result llama_init;  // initialize/release llama_context manually

    llama_model* model;
    llama_context* lctx;
    const llama_vocab* vocab;
    std::vector<llama_token> crop_tokens_lable;

    common_sampler* smpl;
    int32_t n_batch;
    int32_t n_seq_max;
    int32_t n_usage_context;

    // cache
    int32_t kv_cache_seq;

    void* batch_scheduler{nullptr};   // batch scheduler
    void* memory_scheduler{nullptr};  // batch scheduler

    // state for sequences
    std::map<size_t, LlamaSeqState> process_seqs;
    mutable std::mutex process_seqs_mutex;

    std::map<size_t, int32_t> cmpl_to_seq;
    mutable std::mutex cmpl_to_seq_mutex;

    std::string media_marker = MICO_DEFAULT_IMAGE_MARKER;
    common_chat_templates_ptr tmpls;
    llama_tokens antiprompt_tokens;
    int n_threads = 1;

    LlamaMicoContext(common_params& params);
    ~LlamaMicoContext();

    LlamaSeqState& get_seq_state(size_t seq_id);
    int32_t set_seq_id(size_t cmpl_id);
    int32_t get_seq_id(size_t cmpl_id);
    bool erase_seq(int32_t seq_id);

    void init_vision_context(common_params& params);
    bool check_antiprompt(const llama_tokens& generated_tokens);
};

#endif  // MICO_COMMON_H