/* Small portability layer so the protocol files build both in the kernel and in
 * a host unit test. Define SONATINA_KERNEL for the driver build. */
#ifndef SONATINA_PORTABLE_H
#define SONATINA_PORTABLE_H

#ifdef SONATINA_KERNEL
#include <ntddk.h>
typedef UINT8 uint8_t;
typedef UINT16 uint16_t;
typedef UINT32 uint32_t;
typedef UINT64 uint64_t;
typedef INT8 int8_t;
typedef INT16 int16_t;
typedef INT32 int32_t;
typedef INT64 int64_t;
#if !defined(__cplusplus) && !defined(__bool_true_false_are_defined)
typedef BOOLEAN bool;
#define true TRUE
#define false FALSE
#define __bool_true_false_are_defined 1
#endif
#define SONT_MEMCPY(dst, src, len) RtlCopyMemory((dst), (src), (len))
#define SONT_MEMSET(dst, value, len) RtlFillMemory((dst), (len), (value))
#define SONT_ALLOC(size) ExAllocatePool2(POOL_FLAG_NON_PAGED, (size), 'anOS')
#define SONT_FREE(ptr) ExFreePool(ptr)
#else
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define SONT_MEMCPY(dst, src, len) memcpy((dst), (src), (len))
#define SONT_MEMSET(dst, value, len) memset((dst), (value), (len))
#define SONT_ALLOC(size) calloc(1, (size))
#define SONT_FREE(ptr) free(ptr)
#endif

#endif /* SONATINA_PORTABLE_H */
