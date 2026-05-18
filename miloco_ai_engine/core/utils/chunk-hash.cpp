/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

#include "chunk-hash.h"

#include <sstream>
#include <vector>

void StreamHasher::update(const void* data, size_t size) {
    const unsigned char* bytes = static_cast<const unsigned char*>(data);
    for (size_t i = 0; i < size; ++i) {
        hash_ ^= static_cast<uint64_t>(bytes[i]);
        hash_ *= PRIME_;
    }
}

void StreamHasher::update(const std::string& str) { update(str.data(), str.size()); }

std::string StreamHasher::digest() const {
    std::stringstream stream;
    stream << std::hex << std::setfill('0') << std::setw(16) << hash_;
    return stream.str();
}

std::string get_chunk_description(const mtmd_input_chunk* chunk) {
    if (chunk == nullptr) return "";
    std::stringstream stream;

    if (mtmd_input_chunk_get_type(chunk) == MTMD_INPUT_CHUNK_TYPE_TEXT) {
        size_t n_tokens;
        const llama_token* tokens = mtmd_input_chunk_get_tokens_text(chunk, &n_tokens);
        if (tokens && n_tokens > 0) {
            for (size_t j = 0; j < n_tokens; ++j) {
                stream << tokens[j] << ",";
            }
        }
    } else if (mtmd_input_chunk_get_type(chunk) == MTMD_INPUT_CHUNK_TYPE_IMAGE) {
        // For images, we use the image ID as part of the hash
        const mtmd_image_tokens* image_tokens = mtmd_input_chunk_get_tokens_image(chunk);
        if (image_tokens) {
            const char* id = mtmd_image_tokens_get_id(image_tokens);
            stream << "IMG:" << id << ",";
        }
    }
    return stream.str();
}

std::vector<std::string> chunk_hashs(mtmd::input_chunks* input_chunks) {
    StreamHasher md{};
    std::vector<std::string> hashes;
    hashes.reserve(input_chunks->size());
    for (size_t i = 0; i < input_chunks->size(); ++i) {
        const mtmd_input_chunk* chunk = (*input_chunks)[i];
        md.update(get_chunk_description(chunk));
        hashes.push_back(md.digest());
    }
    return hashes;
}
