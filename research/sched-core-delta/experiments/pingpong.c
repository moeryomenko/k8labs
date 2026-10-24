/*
 * pingpong -- interacting producer/consumer round trips across the two vCPUs.
 *
 * Thread A writes a token, thread B reads it, optionally burns CPU, and writes
 * the token back. A records the round-trip time. With --pin the two threads
 * start on different vCPUs, which makes the workload sensitive to cross-vCPU
 * wakeup delay; the steal governor may later consolidate both onto one
 * preferred vCPU, which is exactly the trade-off under study.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_SAMPLES 1000000

struct shared {
    int read_fd;
    int write_fd;
    int burn_us;
    int pin_cpu;
    int pin;
    volatile int failed;
};

static int file_exists(const char *path)
{
    return access(path, F_OK) == 0;
}

static int64_t now_us(void)
{
    struct timespec ts;

    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000LL + (int64_t)ts.tv_nsec / 1000LL;
}

static void add_ns(struct timespec *ts, int64_t ns)
{
    ts->tv_nsec += ns;
    while (ts->tv_nsec >= 1000000000L) {
        ts->tv_nsec -= 1000000000L;
        ts->tv_sec++;
    }
}

static void burn_for_us(int us)
{
    struct timespec end;
    struct timespec now;
    uint64_t x = 0x9e3779b97f4a7c15ULL;

    if (us <= 0)
        return;
    clock_gettime(CLOCK_MONOTONIC, &end);
    add_ns(&end, (int64_t)us * 1000LL);
    for (;;) {
        for (int i = 0; i < 256; i++) {
            x = x * 6364136223846793005ULL + 1442695040888963407ULL;
            x ^= x >> 29;
            __asm__ __volatile__("" : "+r"(x) : : "memory");
        }
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (now.tv_sec > end.tv_sec ||
            (now.tv_sec == end.tv_sec && now.tv_nsec >= end.tv_nsec))
            break;
    }
    __asm__ __volatile__("" : "+r"(x) : : "memory");
}

static void pin_to(int cpu)
{
    cpu_set_t set;

    if (cpu < 0)
        return;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    sched_setaffinity(0, sizeof(set), &set);
}

static void *responder(void *opaque)
{
    struct shared *shared = opaque;
    char token;

    pin_to(shared->pin ? shared->pin_cpu : -1);
    for (;;) {
        ssize_t n = read(shared->read_fd, &token, 1);
        if (n <= 0)
            break;
        burn_for_us(shared->burn_us);
        if (write(shared->write_fd, &token, 1) != 1) {
            shared->failed = 1;
            break;
        }
    }
    close(shared->read_fd);
    close(shared->write_fd);
    return NULL;
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

int main(int argc, char **argv)
{
    double duration_s = 10.0;
    int burn_us = 0;
    int pin = 1;
    int pin_cpu = 1;
    const char *wait_file = NULL;
    const char *output = NULL;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--duration") == 0 && i + 1 < argc) {
            duration_s = atof(argv[++i]);
        } else if (strcmp(argv[i], "--burn-us") == 0 && i + 1 < argc) {
            burn_us = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--pin") == 0 && i + 1 < argc) {
            pin = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--pin-cpu") == 0 && i + 1 < argc) {
            pin_cpu = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--wait-file") == 0 && i + 1 < argc) {
            wait_file = argv[++i];
        } else if (strcmp(argv[i], "--output") == 0 && i + 1 < argc) {
            output = argv[++i];
        } else {
            fprintf(stderr,
                    "usage: %s --duration S --burn-us US --pin 0|1 "
                    "--pin-cpu N --wait-file PATH --output PATH\n",
                    argv[0]);
            return 2;
        }
    }

    if (duration_s <= 0.0 || burn_us < 0 || wait_file == NULL ||
        output == NULL) {
        fprintf(stderr, "invalid arguments\n");
        return 2;
    }

    int ab[2];
    int ba[2];
    if (pipe(ab) != 0 || pipe(ba) != 0) {
        perror("pipe");
        return 1;
    }

    struct shared responder_shared = {
        .read_fd = ab[0],
        .write_fd = ba[1],
        .burn_us = burn_us,
        .pin_cpu = pin_cpu,
        .pin = pin,
        .failed = 0,
    };

    while (!file_exists(wait_file))
        usleep(1000);

    pthread_t thread;
    if (pthread_create(&thread, NULL, responder, &responder_shared) != 0) {
        perror("pthread_create");
        return 1;
    }

    pin_to(pin ? 0 : -1);

    uint64_t *samples = calloc(MAX_SAMPLES, sizeof(*samples));
    if (samples == NULL) {
        perror("calloc");
        return 1;
    }

    int64_t start = now_us();
    int64_t end = start + (int64_t)(duration_s * 1000000.0);
    size_t count = 0;
    char token = 'x';

    while (now_us() < end) {
        int64_t before = now_us();
        if (write(ab[1], &token, 1) != 1)
            break;
        ssize_t n = read(ba[0], &token, 1);
        int64_t after = now_us();
        if (n <= 0)
            break;
        if (count < MAX_SAMPLES)
            samples[count++] = (uint64_t)(after - before);
    }

    close(ab[1]);
    close(ba[0]);
    pthread_join(thread, NULL);

    int64_t elapsed_us = now_us() - start;
    qsort(samples, count, sizeof(*samples), compare_u64);

    FILE *file = fopen(output, "w");
    if (file == NULL) {
        perror(output);
        return 1;
    }
    fprintf(file,
            "{\"round_trips\":%zu,\"elapsed_s\":%.6f,"
            "\"round_trips_per_s\":%.3f,\"burn_us\":%d,\"pinned\":%d,"
            "\"pin_cpu\":%d,\"p50_us\":%llu,\"p99_us\":%llu,"
            "\"p999_us\":%llu,\"max_us\":%llu,\"failed\":%d}\n",
            count, (double)elapsed_us / 1000000.0,
            elapsed_us > 0 ? (double)count * 1000000.0 / (double)elapsed_us : 0.0,
            burn_us, pin, pin_cpu,
            (unsigned long long)percentile(samples, count, 500),
            (unsigned long long)percentile(samples, count, 990),
            (unsigned long long)percentile(samples, count, 999),
            (unsigned long long)(count > 0 ? samples[count - 1] : 0),
            responder_shared.failed);
    fclose(file);
    free(samples);
    return 0;
}
