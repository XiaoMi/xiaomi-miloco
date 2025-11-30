/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */
#ifndef SCHEDULER_TASK_INFO_H
#define SCHEDULER_TASK_INFO_H

#include <atomic>

#include "utils/chunk-hash.h"

enum class TaskStatus {
    PENDING = 0,      // Not yet allocated
    WAIT = 1,         // Allocated, waiting to enter inference queue
    IN_PROGRESS = 2,  // Entered inference queue
    COMPLETED = 3,    // Inference completed
    FAILED = 4,       // Inference failed
};

struct SycChunkTask {
    std::shared_ptr<mtmd_input_chunk> input_chunk;
    std::shared_ptr<std::vector<float>> embeddig;
    std::string chunk_hash{""};
    size_t cmpl_id{0};
    int32_t priority;
    bool is_last_chunk = false;

    std::atomic<TaskStatus> status = TaskStatus::PENDING;

    SycChunkTask(std::shared_ptr<mtmd_input_chunk> chunk, size_t cmpl_id, std::string chunk_hash, int32_t priority)
        : input_chunk(chunk), cmpl_id(cmpl_id), chunk_hash(chunk_hash), priority(priority) {}

    // sort
    bool operator<(const SycChunkTask& other) const {
        if (priority != other.priority) return priority < other.priority;
        return cmpl_id < other.cmpl_id;
    }
    bool operator==(const SycChunkTask& other) const { return cmpl_id == other.cmpl_id && priority == other.priority; }
};

struct BatchSchedulerInput {
    std::vector<std::shared_ptr<SycChunkTask>> input_chunks;

    BatchSchedulerInput(std::shared_ptr<mtmd::input_chunks> chunks, size_t cmpl_id, int32_t prio = 0) {
        if (!chunks) return;
        auto hashs = chunk_hashs(chunks.get());
        for (size_t i = 0; i < chunks->size(); ++i) {
            const mtmd_input_chunk* chunk_ptr = (*chunks)[i];
            // Copy and share to prevent release during inference in other threads
            auto chunk = std::shared_ptr<mtmd_input_chunk>(mtmd_input_chunk_copy(chunk_ptr), mtmd_input_chunk_free);
            input_chunks.emplace_back(std::make_shared<SycChunkTask>(chunk, cmpl_id, hashs[i], prio));
            if (i == chunks->size() - 1) input_chunks.back()->is_last_chunk = true;
        }
    }
};

#endif  // SCHEDULER_TASK_INFO_H