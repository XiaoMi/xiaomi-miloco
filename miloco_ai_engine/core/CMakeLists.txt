cmake_minimum_required(VERSION 3.20)
project(llama_mico LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(LIB_SOURCE_FILES "llama-mico.cpp")

add_library(llama-mico SHARED ${LIB_SOURCE_FILES})

set(THIRD_PARTY_PATH ${PROJECT_SOURCE_DIR}/../../third_party)
set(LLAMA_THIRD_PARTY_PATH ${THIRD_PARTY_PATH}/llama.cpp)

# Ensure runtime output directory is set for subprojects (ggml relies on it for Metal assets)
if(NOT CMAKE_RUNTIME_OUTPUT_DIRECTORY)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
endif()
file(MAKE_DIRECTORY ${CMAKE_RUNTIME_OUTPUT_DIRECTORY})

include_directories(${PROJECT_SOURCE_DIR}
                    ${PROJECT_SOURCE_DIR}/utils
                    ${PROJECT_SOURCE_DIR}/cache_manager
                    ${PROJECT_SOURCE_DIR}/batch_scheduling
                    ${LLAMA_THIRD_PARTY_PATH}
                    ${LLAMA_THIRD_PARTY_PATH}/ggml/include
                    ${LLAMA_THIRD_PARTY_PATH}/src
                    ${LLAMA_THIRD_PARTY_PATH}/mutil-modal
                    ${LLAMA_THIRD_PARTY_PATH}/common
                    ${LLAMA_THIRD_PARTY_PATH}/common/vendor)

add_subdirectory(${LLAMA_THIRD_PARTY_PATH}/ggml ${CMAKE_CURRENT_BINARY_DIR}/llama_third_party/ggml)
add_subdirectory(${LLAMA_THIRD_PARTY_PATH}/src  ${CMAKE_CURRENT_BINARY_DIR}/llama_third_party/src)

target_link_libraries(llama-mico PRIVATE llama ggml)

add_subdirectory(utils)
add_subdirectory(cache_manager)
add_subdirectory(batch_scheduling)
# add_subdirectory(mutil-modal)


install(TARGETS llama-mico DESTINATION lib)
