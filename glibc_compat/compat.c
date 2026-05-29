/*
 * glibc_compat.c
 * Provides GLIBC_2.33 and GLIBC_2.34 versioned symbols for lpminer on Ubuntu 20.04 (glibc 2.31).
 * Compile: gcc -shared -fPIC -o compat.so compat.c -lpthread -ldl -lrt
 * Use:     LD_PRELOAD=/tmp/compat.so /tmp/lpminer/lpminer --help
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <pthread.h>
#include <semaphore.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <stddef.h>
#include <stdlib.h>

/* Helper macro to define a GLIBC_2.34 wrapper using RTLD_NEXT */
#define WRAP234_R(ret, name, args_decl, args_call) \
    ret __compat_##name args_decl { \
        typedef ret (*fn_t) args_decl; \
        static fn_t _fn; \
        if (!_fn) _fn = (fn_t)dlsym(RTLD_NEXT, #name); \
        return _fn args_call; \
    } \
    __asm__(".symver __compat_" #name "," #name "@GLIBC_2.34");

#define WRAP234_V(name, args_decl, args_call) \
    void __compat_##name args_decl { \
        typedef void (*fn_t) args_decl; \
        static fn_t _fn; \
        if (!_fn) _fn = (fn_t)dlsym(RTLD_NEXT, #name); \
        _fn args_call; \
    } \
    __asm__(".symver __compat_" #name "," #name "@GLIBC_2.34");

/* ---------- GLIBC_2.34: pthread ---------- */

WRAP234_R(int, pthread_create,
    (pthread_t *t, const pthread_attr_t *a, void *(*f)(void *), void *arg),
    (t, a, f, arg))

WRAP234_R(int, pthread_join,
    (pthread_t t, void **r),
    (t, r))

WRAP234_R(int, pthread_detach,
    (pthread_t t),
    (t))

WRAP234_R(int, pthread_kill,
    (pthread_t t, int sig),
    (t, sig))

WRAP234_R(int, pthread_once,
    (pthread_once_t *o, void (*f)(void)),
    (o, f))

WRAP234_R(int, pthread_key_create,
    (pthread_key_t *k, void (*d)(void *)),
    (k, d))

WRAP234_R(int, pthread_key_delete,
    (pthread_key_t k),
    (k))

WRAP234_R(int, pthread_getspecific,
    (pthread_key_t k),
    (k))

WRAP234_R(int, pthread_setspecific,
    (pthread_key_t k, const void *v),
    (k, v))

WRAP234_R(int, pthread_condattr_setpshared,
    (pthread_condattr_t *a, int s),
    (a, s))

WRAP234_R(int, pthread_mutexattr_settype,
    (pthread_mutexattr_t *a, int t),
    (a, t))

WRAP234_R(int, pthread_mutexattr_setpshared,
    (pthread_mutexattr_t *a, int s),
    (a, s))

WRAP234_R(int, pthread_mutexattr_destroy,
    (pthread_mutexattr_t *a),
    (a))

WRAP234_R(int, pthread_rwlock_init,
    (pthread_rwlock_t *rw, const pthread_rwlockattr_t *a),
    (rw, a))

WRAP234_R(int, pthread_rwlock_destroy,
    (pthread_rwlock_t *rw),
    (rw))

WRAP234_R(int, pthread_rwlock_rdlock,
    (pthread_rwlock_t *rw),
    (rw))

WRAP234_R(int, pthread_rwlock_tryrdlock,
    (pthread_rwlock_t *rw),
    (rw))

WRAP234_R(int, pthread_rwlock_timedrdlock,
    (pthread_rwlock_t *rw, const struct timespec *t),
    (rw, t))

WRAP234_R(int, pthread_rwlock_wrlock,
    (pthread_rwlock_t *rw),
    (rw))

WRAP234_R(int, pthread_rwlock_trywrlock,
    (pthread_rwlock_t *rw),
    (rw))

WRAP234_R(int, pthread_rwlock_timedwrlock,
    (pthread_rwlock_t *rw, const struct timespec *t),
    (rw, t))

WRAP234_R(int, pthread_rwlock_unlock,
    (pthread_rwlock_t *rw),
    (rw))

WRAP234_R(int, pthread_rwlockattr_init,
    (pthread_rwlockattr_t *a),
    (a))

WRAP234_R(int, pthread_rwlockattr_destroy,
    (pthread_rwlockattr_t *a),
    (a))

WRAP234_R(int, pthread_rwlockattr_setpshared,
    (pthread_rwlockattr_t *a, int s),
    (a, s))

/* ---------- GLIBC_2.34: semaphore ---------- */

WRAP234_R(int, sem_init,
    (sem_t *s, int p, unsigned int v),
    (s, p, v))

WRAP234_R(int, sem_destroy,
    (sem_t *s),
    (s))

WRAP234_R(int, sem_wait,
    (sem_t *s),
    (s))

WRAP234_R(int, sem_trywait,
    (sem_t *s),
    (s))

WRAP234_R(int, sem_timedwait,
    (sem_t *s, const struct timespec *t),
    (s, t))

WRAP234_R(int, sem_post,
    (sem_t *s),
    (s))

/* ---------- GLIBC_2.34: dl* ---------- */

WRAP234_R(void *, dlopen,
    (const char *f, int m),
    (f, m))

WRAP234_R(void *, dlsym,
    (void *h, const char *n),
    (h, n))

WRAP234_R(void *, dlvsym,
    (void *h, const char *n, const char *v),
    (h, n, v))

WRAP234_R(int, dlclose,
    (void *h),
    (h))

WRAP234_R(char *, dlerror, (void), ())

WRAP234_R(int, dladdr,
    (const void *a, Dl_info *i),
    (a, i))

/* dlmopen needs extra header */
#include <link.h>
WRAP234_R(void *, dlmopen,
    (Lmid_t lm, const char *path, int mode),
    (lm, path, mode))

/* ---------- GLIBC_2.34: shm ---------- */
#include <fcntl.h>
WRAP234_R(int, shm_open,
    (const char *n, int o, mode_t m),
    (n, o, m))

WRAP234_R(int, shm_unlink,
    (const char *n),
    (n))

/* ---------- GLIBC_2.34: __libc_start_main ---------- */
/* This is the trickiest one. We forward to the old GLIBC_2.2.5 version. */
extern int __libc_start_main_real(
    int (*main)(int, char **, char **),
    int argc, char **argv,
    void (*init)(void), void (*fini)(void),
    void (*rtld_fini)(void), void *stack_end)
    __asm("__libc_start_main");

int __compat_libc_start_main(
    int (*main)(int, char **, char **),
    int argc, char **argv,
    void (*init)(void), void (*fini)(void),
    void (*rtld_fini)(void), void *stack_end)
{
    return __libc_start_main_real(main, argc, argv, init, fini, rtld_fini, stack_end);
}
__asm__(".symver __compat_libc_start_main,__libc_start_main@GLIBC_2.34");

/* ---------- GLIBC_2.33: stat, fstat ---------- */
/* In glibc 2.33, stat/fstat got new direct syscall wrappers.
 * We forward to the glibc 2.31 versions (__xstat/__fxstat). */
extern int __xstat(int ver, const char *path, struct stat *buf);
extern int __fxstat(int ver, int fd, struct stat *buf);
#define _STAT_VER 1

int __compat_stat(const char *path, struct stat *buf) {
    return __xstat(_STAT_VER, path, buf);
}
__asm__(".symver __compat_stat,stat@GLIBC_2.33");

int __compat_fstat(int fd, struct stat *buf) {
    return __fxstat(_STAT_VER, fd, buf);
}
__asm__(".symver __compat_fstat,fstat@GLIBC_2.33");
