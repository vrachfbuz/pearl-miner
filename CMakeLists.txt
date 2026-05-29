cmake_minimum_required(VERSION 3.20)
project(pearl_miner LANGUAGES CXX CUDA)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CUDA_STANDARD 17)

# Целевая архитектура: sm_75 (Turing / CMP 40HX)
# Добавить sm_86 и выше если нужна совместимость
set(CMAKE_CUDA_ARCHITECTURES 75)

find_package(CUDAToolkit REQUIRED)

# BLAKE3 (скачать: https://github.com/BLAKE3-team/BLAKE3)
# Ожидаем blake3.c и blake3.h в ./third_party/blake3/
add_library(blake3 STATIC
    third_party/blake3/blake3.c
    third_party/blake3/blake3_dispatch.c
    third_party/blake3/blake3_portable.c
)
target_include_directories(blake3 PUBLIC third_party/blake3)

# Основная shared library (Python ctypes будет её грузить)
add_library(pearl_kernel SHARED
    kernel/noisy_gemm.cu
    kernel/blake3_host.c     # обёртка blake3_keyed() для вызова из CUDA
)
target_link_libraries(pearl_kernel
    PRIVATE blake3 CUDA::cudart
)
set_target_properties(pearl_kernel PROPERTIES
    CUDA_SEPARABLE_COMPILATION ON
    OUTPUT_NAME "pearl_kernel"
    PREFIX ""
)

# Тест корректности (CPU-сравнение)
add_executable(test_kernel
    tests/test_noisy_gemm.cpp
)
target_link_libraries(test_kernel PRIVATE pearl_kernel blake3)
