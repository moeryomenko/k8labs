/*
 * wakeup_latency -- periodic 1 ms timer probe.
 *
 * Waits for a go-file, then uses absolute CLOCK_MONOTONIC sleeps so the
 * expected wakeup time does not drift. The observed delay is how long after
 * the deadline the thread actually ran. Reports p50/p99/max in microseconds.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_SAMPLES 200000

static int file_exists(const char *path)
{
    return access(path, F_OK) == 0;
}

static int compare_u64(const void *left, const void *right)
{
    uint64_t a = *(const uint64_t *)left;
    uint64_t b = *(const uint64_t *)right;

    return (a > b) - (a < b);
}

static void add_ns(struct timespec *ts, long ns)
{
    ts->tv_nsec += ns;
    while (ts->tv_nsec >= 1000000000L) {
        ts->tv_nsec -= 1000000000L;
        ts->tv_sec++;
    }
}

int main(int argc, char **argv)
{
    double duration_s = 10.0;
    long interval_ns = 1000000L;
    char *wait_file = NULL;
    char *output = NULL;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--duration") == 0 && i + 1 < argc) {
            duration_s = atof(argv[++i]);
        } else if (strcmp(argv[i], "--interval-us") == 0 && i + 1 < argc) {
            interval_ns = atol(argv[++i]) * 1000L;
        } else if (strcmp(argv[i], "--wait-file") == 0 && i + 1 < argc) {
            wait_file = argv[++i];
        } else if (strcmp(argv[i], "--output") == 0 && i + 1 < argc) {
            output = argv[++i];
        } else {
            fprintf(stderr, "usage: %s --duration S --interval-us US --wait-file PATH --output PATH\n", argv[0]);
            return 2;
        }
    }

    if (duration_s <= 0.0 || interval_ns <= 0 || wait_file == NULL || output == NULL) {
        fprintf(stderr, "invalid arguments\n");
        return 2;
    }

    uint64_t *samples = calloc(MAX_SAMPLES, sizeof(*samples));
    if (samples == NULL) {
        perror("calloc");
        return 1;
    }

    while (!file_exists(wait_file))
        usleep(1000);

    struct timespec next;
    struct timespec end;
    size_t count = 0;
    uint64_t max_delay = 0;

    clock_gettime(CLOCK_MONOTONIC, &next);
    end = next;
    end.tv_sec += (time_t)duration_s;

    while (1) {
        add_ns(&next, interval_ns);
        if (next.tv_sec > end.tv_sec ||
            (next.tv_sec == end.tv_sec && next.tv_nsec >= end.tv_nsec))
            break;

        int rc;
        do {
            rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
        } while (rc == EINTR);

        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);

        int64_t delay_ns = (int64_t)(now.tv_sec - next.tv_sec) * 1000000000LL +
                           (int64_t)(now.tv_nsec - next.tv_nsec);
        if (delay_ns < 0)
            delay_ns = 0;

        uint64_t delay_us = (uint64_t)delay_ns / 1000ULL;
        if (count < MAX_SAMPLES)
            samples[count++] = delay_us;
        if (delay_us > max_delay)
            max_delay = delay_us;
    }

    qsort(samples, count, sizeof(*samples), compare_u64);

    uint64_t p50 = count == 0 ? 0 : samples[(count - 1) * 50 / 100];
    uint64_t p99 = count == 0 ? 0 : samples[(count - 1) * 99 / 100];
    uint64_t p999 = count == 0 ? 0 : samples[(count - 1) * 999 / 1000];

    FILE *file = fopen(output, "w");
    if (file == NULL) {
        perror(output);
        return 1;
    }
    fprintf(file,
            "{\"samples\":%zu,\"p50_us\":%llu,\"p99_us\":%llu,\"p999_us\":%llu,\"max_us\":%llu}\n",
            count, (unsigned long long)p50, (unsigned long long)p99,
            (unsigned long long)p999, (unsigned long long)max_delay);
    fclose(file);
    free(samples);
    return 0;
}
