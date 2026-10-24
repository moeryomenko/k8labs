/*
 * periodic_burn -- periodic short CPU bursts against a loaded node.
 *
 * Each thread waits for the go-file, then wakes on an absolute CLOCK_MONOTONIC
 * deadline every --period-us, burns for --burn-us, and records the delay
 * between the deadline and the moment it actually started running. A burn that
 * overruns the next deadline skips that period and counts it. The workload is
 * started at the measurement boundary, not during the warmup.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <math.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_SAMPLES 200000

struct thread_arg {
    int id;
    int threads;
    long period_us;
    long burn_us;
    double duration_s;
    const char *wait_file;
    struct thread_result *result;
};

struct thread_result {
    uint64_t samples;
    uint64_t iterations;
    uint64_t overruns;
    uint64_t missed_periods;
    uint64_t *delays;
    size_t count;
    uint64_t accumulator;
};

static volatile uint64_t sink;

static int file_exists(const char *path)
{
    return access(path, F_OK) == 0;
}

static void add_us(struct timespec *ts, long us)
{
    ts->tv_nsec += us * 1000L;
    while (ts->tv_nsec >= 1000000000L) {
        ts->tv_nsec -= 1000000000L;
        ts->tv_sec++;
    }
}

static int64_t diff_us(const struct timespec *a, const struct timespec *b)
{
    return (int64_t)(a->tv_sec - b->tv_sec) * 1000000LL +
           (int64_t)(a->tv_nsec - b->tv_nsec) / 1000LL;
}

static int cmp_ts(const struct timespec *a, const struct timespec *b)
{
    if (a->tv_sec != b->tv_sec)
        return a->tv_sec < b->tv_sec ? -1 : 1;
    if (a->tv_nsec != b->tv_nsec)
        return a->tv_nsec < b->tv_nsec ? -1 : 1;
    return 0;
}

static void burn_until(const struct timespec *end)
{
    uint64_t x = 0x9e3779b97f4a7c15ULL + (uintptr_t)end;
    struct timespec now;

    for (;;) {
        for (int i = 0; i < 256; i++) {
            x = x * 6364136223846793005ULL + 1442695040888963407ULL;
            x ^= x >> 29;
            __asm__ __volatile__("" : "+r"(x) : : "memory");
        }
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (cmp_ts(&now, end) >= 0)
            break;
    }
    sink ^= x;
}

static int compare_u64(const void *left, const void *right)
{
    uint64_t a = *(const uint64_t *)left;
    uint64_t b = *(const uint64_t *)right;

    return (a > b) - (a < b);
}

static uint64_t percentile(const uint64_t *values, size_t count, int permille)
{
    if (count == 0)
        return 0;
    return values[(count - 1) * (size_t)permille / 1000];
}

static void *worker(void *opaque)
{
    struct thread_arg *arg = opaque;
    struct thread_result *result = arg->result;
    struct timespec next;
    struct timespec end;
    struct timespec now;

    result->delays = calloc(MAX_SAMPLES, sizeof(*result->delays));
    if (result->delays == NULL)
        return NULL;

    while (!file_exists(arg->wait_file))
        usleep(1000);

    clock_gettime(CLOCK_MONOTONIC, &next);
    add_us(&next, arg->period_us);
    end = next;
    add_us(&end, (long)(arg->duration_s * 1000000.0));

    while (cmp_ts(&next, &end) < 0) {
        int rc;

        do {
            rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
        } while (rc == EINTR);

        clock_gettime(CLOCK_MONOTONIC, &now);
        int64_t delay = diff_us(&now, &next);
        if (delay < 0)
            delay = 0;
        if (result->count < MAX_SAMPLES)
            result->delays[result->count++] = (uint64_t)delay;
        result->samples++;

        struct timespec burn_end = now;
        add_us(&burn_end, arg->burn_us);
        burn_until(&burn_end);
        result->iterations++;

        add_us(&next, arg->period_us);
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (cmp_ts(&next, &now) <= 0) {
            result->overruns++;
            while (cmp_ts(&next, &now) <= 0) {
                result->missed_periods++;
                add_us(&next, arg->period_us);
            }
        }
    }

    result->accumulator = sink;
    return NULL;
}

static void free_result(struct thread_result *result)
{
    free(result->delays);
    result->delays = NULL;
}

int main(int argc, char **argv)
{
    int threads = 1;
    long period_us = 2000;
    long burn_us = 200;
    double duration_s = 10.0;
    const char *wait_file = NULL;
    const char *output = NULL;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--threads") == 0 && i + 1 < argc) {
            threads = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--period-us") == 0 && i + 1 < argc) {
            period_us = atol(argv[++i]);
        } else if (strcmp(argv[i], "--burn-us") == 0 && i + 1 < argc) {
            burn_us = atol(argv[++i]);
        } else if (strcmp(argv[i], "--duration") == 0 && i + 1 < argc) {
            duration_s = atof(argv[++i]);
        } else if (strcmp(argv[i], "--wait-file") == 0 && i + 1 < argc) {
            wait_file = argv[++i];
        } else if (strcmp(argv[i], "--output") == 0 && i + 1 < argc) {
            output = argv[++i];
        } else {
            fprintf(stderr,
                    "usage: %s --threads N --period-us US --burn-us US "
                    "--duration S --wait-file PATH --output PATH\n",
                    argv[0]);
            return 2;
        }
    }

    if (threads < 1 || period_us <= 0 || burn_us < 0 || duration_s <= 0.0 ||
        wait_file == NULL || output == NULL) {
        fprintf(stderr, "invalid arguments\n");
        return 2;
    }
    if (burn_us >= period_us) {
        fprintf(stderr, "--burn-us must be smaller than --period-us\n");
        return 2;
    }

    pthread_t *ids = calloc((size_t)threads, sizeof(*ids));
    struct thread_arg *args = calloc((size_t)threads, sizeof(*args));
    struct thread_result *results = calloc((size_t)threads, sizeof(*results));
    if (ids == NULL || args == NULL || results == NULL) {
        perror("calloc");
        return 1;
    }

    for (int i = 0; i < threads; i++) {
        args[i].id = i;
        args[i].threads = threads;
        args[i].period_us = period_us;
        args[i].burn_us = burn_us;
        args[i].duration_s = duration_s;
        args[i].wait_file = wait_file;
        args[i].result = &results[i];
        if (pthread_create(&ids[i], NULL, worker, &args[i]) != 0) {
            perror("pthread_create");
            return 1;
        }
    }

    for (int i = 0; i < threads; i++)
        pthread_join(ids[i], NULL);

    size_t total = 0;
    uint64_t iterations = 0;
    uint64_t overruns = 0;
    uint64_t missed = 0;
    for (int i = 0; i < threads; i++) {
        total += results[i].count;
        iterations += results[i].iterations;
        overruns += results[i].overruns;
        missed += results[i].missed_periods;
    }

    uint64_t *combined = calloc(total > 0 ? total : 1, sizeof(*combined));
    if (combined == NULL) {
        perror("calloc");
        return 1;
    }
    size_t offset = 0;
    for (int i = 0; i < threads; i++) {
        if (results[i].count > 0) {
            memcpy(&combined[offset], results[i].delays,
                   results[i].count * sizeof(*combined));
            offset += results[i].count;
        }
    }
    qsort(combined, total, sizeof(*combined), compare_u64);

    double mean_iterations = 0.0;
    for (int i = 0; i < threads; i++)
        mean_iterations += (double)results[i].iterations;
    mean_iterations /= (double)threads;

    double variance = 0.0;
    for (int i = 0; i < threads; i++) {
        double delta = (double)results[i].iterations - mean_iterations;
        variance += delta * delta;
    }
    variance /= (double)threads;
    double cv = mean_iterations > 0.0 ? sqrt(variance) / mean_iterations : 0.0;

    FILE *file = fopen(output, "w");
    if (file == NULL) {
        perror(output);
        return 1;
    }
    fprintf(file,
            "{\"threads\":%d,\"period_us\":%ld,\"burn_us\":%ld,"
            "\"samples\":%zu,\"iterations\":%llu,\"overruns\":%llu,"
            "\"missed_periods\":%llu,\"p50_us\":%llu,\"p99_us\":%llu,"
            "\"p999_us\":%llu,\"max_us\":%llu,\"thread_cv\":%.6f,"
            "\"thread_iters\":[",
            threads, period_us, burn_us, total, (unsigned long long)iterations,
            (unsigned long long)overruns, (unsigned long long)missed,
            (unsigned long long)percentile(combined, total, 500),
            (unsigned long long)percentile(combined, total, 990),
            (unsigned long long)percentile(combined, total, 999),
            (unsigned long long)(total > 0 ? combined[total - 1] : 0), cv);
    for (int i = 0; i < threads; i++) {
        fprintf(file, "%s%llu", i == 0 ? "" : ",",
                (unsigned long long)results[i].iterations);
    }
    fprintf(file, "],\"per_thread\":[");
    for (int i = 0; i < threads; i++) {
        qsort(results[i].delays, results[i].count, sizeof(*results[i].delays),
              compare_u64);
        fprintf(file,
                "%s{\"id\":%d,\"samples\":%llu,\"iterations\":%llu,"
                "\"overruns\":%llu,\"missed_periods\":%llu,"
                "\"p50_us\":%llu,\"p99_us\":%llu,\"p999_us\":%llu,"
                "\"max_us\":%llu}",
                i == 0 ? "" : ",", i,
                (unsigned long long)results[i].samples,
                (unsigned long long)results[i].iterations,
                (unsigned long long)results[i].overruns,
                (unsigned long long)results[i].missed_periods,
                (unsigned long long)percentile(results[i].delays,
                                               results[i].count, 500),
                (unsigned long long)percentile(results[i].delays,
                                               results[i].count, 990),
                (unsigned long long)percentile(results[i].delays,
                                               results[i].count, 999),
                (unsigned long long)(results[i].count > 0
                                         ? results[i].delays[results[i].count - 1]
                                         : 0));
    }
    fprintf(file, "]}\n");
    fclose(file);

    for (int i = 0; i < threads; i++)
        free_result(&results[i]);
    free(combined);
    free(ids);
    free(args);
    free(results);
    return 0;
}
