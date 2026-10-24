/*
 * cpu_burn -- small, dependency-free CPU throughput workload.
 *
 * Starts N pthreads, waits for a go-file (so the runner can move the whole
 * thread group into its cgroup first), then spins for a fixed duration and
 * reports the completed outer-loop iterations. The arithmetic deliberately
 * feeds a volatile sink so the compiler cannot remove it.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

struct args {
    int threads;
    double duration_s;
    char *wait_file;
};

struct result {
    uint64_t iterations;
    uint64_t accumulator;
};

static volatile uint64_t sink;

static int file_exists(const char *path)
{
    return access(path, F_OK) == 0;
}

static double monotonic_seconds(void)
{
    struct timespec ts;

    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1000000000.0;
}

static void *worker(void *opaque)
{
    const struct args *args = opaque;
    struct result *result = calloc(1, sizeof(*result));
    uint64_t x = 0x9e3779b97f4a7c15ULL + (uintptr_t)pthread_self();
    uint64_t iterations = 0;
    double deadline;

    if (result == NULL)
        return NULL;

    while (!file_exists(args->wait_file))
        usleep(1000);

    deadline = monotonic_seconds() + args->duration_s;
    while (monotonic_seconds() < deadline) {
        for (int i = 0; i < 10000; i++) {
            x = x * 6364136223846793005ULL + 1442695040888963407ULL;
            x ^= x >> 29;
            __asm__ __volatile__("" : "+r"(x) : : "memory");
        }
        iterations++;
    }

    result->iterations = iterations;
    result->accumulator = x;
    sink ^= x;
    return result;
}

int main(int argc, char **argv)
{
    struct args args = {.threads = 1, .duration_s = 10.0, .wait_file = NULL};
    char *output = NULL;
    pthread_t *threads;
    uint64_t iterations = 0;
    uint64_t accumulator = 0;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--threads") == 0 && i + 1 < argc) {
            args.threads = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--duration") == 0 && i + 1 < argc) {
            args.duration_s = atof(argv[++i]);
        } else if (strcmp(argv[i], "--wait-file") == 0 && i + 1 < argc) {
            args.wait_file = argv[++i];
        } else if (strcmp(argv[i], "--output") == 0 && i + 1 < argc) {
            output = argv[++i];
        } else {
            fprintf(stderr, "usage: %s --threads N --duration S --wait-file PATH --output PATH\n", argv[0]);
            return 2;
        }
    }

    if (args.threads < 1 || args.duration_s <= 0.0 || args.wait_file == NULL || output == NULL) {
        fprintf(stderr, "invalid arguments\n");
        return 2;
    }

    threads = calloc((size_t)args.threads, sizeof(*threads));
    if (threads == NULL) {
        perror("calloc");
        return 1;
    }

    for (int i = 0; i < args.threads; i++) {
        if (pthread_create(&threads[i], NULL, worker, &args) != 0) {
            perror("pthread_create");
            return 1;
        }
    }

    for (int i = 0; i < args.threads; i++) {
        struct result *result = NULL;

        if (pthread_join(threads[i], (void **)&result) != 0) {
            perror("pthread_join");
            return 1;
        }
        if (result != NULL) {
            iterations += result->iterations;
            accumulator ^= result->accumulator;
            free(result);
        }
    }

    FILE *file = fopen(output, "w");
    if (file == NULL) {
        perror(output);
        return 1;
    }
    fprintf(file, "{\"threads\":%d,\"iterations\":%llu,\"accumulator\":%llu}\n",
            args.threads, (unsigned long long)iterations,
            (unsigned long long)accumulator);
    fclose(file);
    free(threads);
    return 0;
}
