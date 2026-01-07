/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */
#ifndef CHUNK_HASH_H
#define CHUNK_HASH_H

#include <cstdint>
#include <iomanip>
#include <ios>
#include <string>

#include "mutil-modal/mtmd.h"

class StreamHasher {
  public:
    void update(const void* data, size_t size);
    void update(const std::string& str);
    std::string digest() const;

  private:
    static constexpr uint64_t PRIME_ = 0x01000193;
    static constexpr uint64_t INITIAL_HASH_ = 0x811c9dc5;
    uint64_t hash_ = INITIAL_HASH_;
};

std::string get_chunk_description(const mtmd_input_chunk* chunk);

std::vector<std::string> chunk_hashs(mtmd::input_chunks* input_chunks);

#endif  // CHUNK_HASH_H
