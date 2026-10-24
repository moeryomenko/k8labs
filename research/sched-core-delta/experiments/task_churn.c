/*
 * task_churn -- short-lived task creation and exit at a fixed rate.
 *
 * The main thread creates one pthread every 1/rate seconds. Each thread
 * records the delay between the intended spawn moment and the moment it first
 * runs (scheduler start delay), burns for --work-us, and records the total
 * completion time. The aggregate distributions expose per-task scheduler cost
 * and placement delay under CPU pressure.
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

#define RING 256
#define MAX_SAMPLES 1000000

struct sample {
    uint64_t start_us;
    uint64_t completion_us;
};

struct task_arg {
    int index;
    struct timespec spawn;
    long work_us;
    struct sample *samples;
};

static volatile uint64_t sink;

static int file_exists(const char *path)
{
    return access(path, F_OK) == 0;
}

static void add_ns(struct timespec *ts, int64_t ns)
{
    ts->tv_nsec += ns;
    while (ts->tv_nsec >= 1000000000L) {
        ts->tv_nsec -= 1000000000L;
        ts->tv_sec++;
    }
    while (ts->tv_nsec < 0) {
        ts->tv_nsec += 1000000000L;
        ts->tv_sec--;
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

static void burn_for_us(long us)
{
    struct timespec end;
    struct timespec now;
    uint64_t x = 0x9e3779b97f4a7c15ULL;

    clock_gettime(CLOCK_MONOTONIC, &end);
    add_ns(&end, (int64_t)us * 1000LL);
    for (;;) {
        for (int i = 0; i < 256; i++) {
            x = x * 6364136223846793005ULL + 1442695040888963407ULL;
            x ^= x >> 29;
            __asm__ __volatile__("" : "+r"(x) : : "memory");
        }
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (cmp_ts(&now, &end) >= 0)
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
    struct task_arg *arg = opaque;
    struct timespec entry;

    clock_gettime(CLOCK_MONOTONIC, &entry);
    int64_t start_us = diff_us(&entry, &arg->spawn);
    if (start_us < 0)
        start_us = 0;

    burn_for_us(arg->work_us);

    struct timespec done;
    clock_gettime(CLOCK_MONOTONIC, &done);
    int64_t completion_us = diff_us(&done, &arg->spawn);
    if (completion_us < 0)
        completion_us = 0;

    arg->samples[arg->index].start_us = (uint64_t)start_us;
    arg->samples[arg->index].completion_us = (uint64_t)completion_us;
    return NULL;
}

int main(int argc, char **argv)
{
    long rate = 200;
    double duration_s = 10.0;
    long work_us = 20;
    const char *wait_file = NULL;
    const char *output = NULL;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--rate") == 0 && i + 1 < argc) {
            rate = atol(argv[++i]);
        } else if (strcmp(argv[i], "--duration") == 0 && i + 1 < argc) {
            duration_s = atof(argv[++i]);
        } else if (strcmp(argv[i], "--work-us") == 0 && i + 1 < argc) {
            work_us = atol(argv[++i]);
        } else if (strcmp(argv[i], "--wait-file") == 0 && i + 1 < argc) {
            wait_file = argv[++i];
        } else if (strcmp(argv[i], "--output") == 0 && i + 1 < argc) {
            output = argv[++i];
        } else {
            fprintf(stderr,
                    "usage: %s --rate N --duration S --work-us US "
                    "--wait-file PATH --output PATH\n",
                    argv[0]);
            return 2;
        }
    }

    if (rate <= 0 || duration_s <= 0.0 || work_us < 0 || wait_file == NULL ||
        output == NULL) {
        fprintf(stderr, "invalid arguments\n");
        return 2;
    }

    uint64_t planned = (uint64_t)((double)rate * duration_s);
    if (planned == 0)
        planned = 1;
    uint64_t capacity = planned + 1024;
    if (capacity > MAX_SAMPLES)
        capacity = MAX_SAMPLES;

    struct sample *samples = calloc(capacity, sizeof(*samples));
    struct task_arg *args = calloc(capacity, sizeof(*args));
    pthread_t ring[RING];
    memset(ring, 0, sizeof(ring));
    if (samples == NULL || args == NULL) {
        perror("calloc");
        return 1;
    }

    while (!file_exists(wait_file))
        usleep(1000);

    struct timespec start;
    clock_gettime(CLOCK_MONOTONIC, &start);
    int64_t interval_ns = 1000000000LL / rate;
    uint64_t created = 0;

    for (uint64_t i = 0; i < planned; i++) {
        struct timespec target = start;
        add_ns(&target, (int64_t)i * interval_ns);

        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (cmp_ts(&now, &target) < 0) {
            int rc;
            do {
                rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &target,
                                     NULL);
            } while (rc == EINTR);
        }

        clock_gettime(CLOCK_MONOTONIC, &args[i].spawn);
        args[i].index = (int)i;
        args[i].work_us = work_us;
        args[i].samples = samples;

        if (i >= RING) {
            if (pthread_join(ring[i % RING], NULL) != 0)
                break;
        }
        if (pthread_create(&ring[i % RING], NULL, worker, &args[i]) != 0)
            break;
        created++;
    }

    uint64_t join_from = created > RING ? created - RING : 0;
    for (uint64_t i = join_from; i < created; i++)
        pthread_join(ring[i % RING], NULL);

    uint64_t completed = created;
    uint64_t *starts = calloc(completed > 0 ? completed : 1, sizeof(*starts));
    uint64_t *completions =
        calloc(completed > 0 ? completed : 1, sizeof(*completions));
    if (starts == NULL || completions == NULL) {
        perror("calloc");
        return 1;
    }
    for (uint64_t i = 0; i < completed; i++) {
        starts[i] = samples[i].start_us;
        completions[i] = samples[i].completion_us;
    }
    qsort(starts, completed, sizeof(*starts), compare_u64);
    qsort(completions, completed, sizeof(*completions), compare_u64);

    struct timespec end;
    clock_gettime(CLOCK_MONOTONIC, &end);
    double elapsed = (double)(end.tv_sec - start.tv_sec) +
                     (double)(end.tv_nsec - start.tv_nsec) / 1000000000.0;
    double actual_rate = elapsed > 0.0 ? (double)completed / elapsed : 0.0;

    FILE *file = fopen(output, "w");
    if (file == NULL) {
        perror(output);
        return 1;
    }
    fprintf(file,
            "{\"rate\":%ld,\"work_us\":%ld,\"created\":%llu,"
            "\"completed\":%llu,\"elapsed_s\":%.6f,\"tasks_per_s\":%.3f,"
            "\"p50_us\":%llu,\"p99_us\":%llu,\"p999_us\":%llu,\"max_us\":%llu,"
            "\"completion_p50_us\":%llu,\"completion_p99_us\":%llu,"
            "\"completion_p999_us\":%llu,\"completion_max_us\":%llu}\n",
            rate, work_us, (unsigned long long)created,
            (unsigned long long)completed, elapsed, actual_rate,
            (unsigned long long)percentile(starts, completed, 500),
            (unsigned long long)percentile(starts, completed, 990),
            (unsigned long long)percentile(starts, completed, 999),
            (unsigned long long)(completed > 0 ? starts[completed - 1] : 0),
            (unsigned long long)percentile(completions, completed, 500),
            (unsigned long long)percentile(completions, completed, 990),
            (unsigned long long)percentile(completions, completed, 999),
            (unsigned long long)(completed > 0 ? completions[completed - 1] : 0));
    fclose(file);

    free(starts);
    free(completions);
    free(samples);
    free(args);
    return 0;
}
