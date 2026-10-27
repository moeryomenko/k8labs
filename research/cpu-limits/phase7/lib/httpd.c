/*
 * Minimal static HTTP server for the Phase 7 incident simulation.
 *
 * The cluster can no longer pull the nginx image it used earlier: the image
 * was garbage-collected from the node during a disk-pressure episode and
 * Docker Hub answers anonymous pulls with a rate-limit error. The incident
 * scenarios only need a target whose CPU cost per request is stable and whose
 * latency the client can measure, so this server replaces nginx there.
 *
 * It answers every GET with the same fixed body and keeps the connection
 * alive, which is what the load generator's keep-alive loop expects.
 *
 * A plain static response costs about 20 microseconds of CPU on this node,
 * which a 120m quota can serve tens of thousands of times a second: the quota
 * never binds and the incident never happens. --work-us adds a deterministic
 * busy wait to every request, so the CPU cost per request is a known number
 * and a quota of 120m saturates at 120ms/--work-us requests per second. The
 * incidents use 100 microseconds, which puts the knee at about 1200 req/s.
 *
 * Build: gcc -O2 -static -pthread -o httpd httpd.c
 * Usage: httpd --port 80 [--body-bytes 1024] [--work-us 100]
 */

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define MAX_REQUEST 8192
#define MAX_BODY 65536

static char body[MAX_BODY];
static size_t body_len = 1024;
static char header[512];
static size_t header_len;
static long work_us;

static int send_all(int fd, const char *buffer, size_t length)
{
	size_t sent = 0;

	while (sent < length) {
		ssize_t written = write(fd, buffer + sent, length - sent);
		if (written < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		sent += (size_t)written;
	}
	return 0;
}

/* Read until the blank line that ends the request headers. */
static int read_request(int fd)
{
	char buffer[MAX_REQUEST];
	size_t used = 0;

	while (used < sizeof(buffer) - 1) {
		ssize_t got = read(fd, buffer + used, sizeof(buffer) - 1 - used);
		if (got < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		if (got == 0)
			return used ? 1 : -1;
		used += (size_t)got;
		buffer[used] = '\0';
		if (strstr(buffer, "\r\n\r\n") || strstr(buffer, "\n\n"))
			return 1;
	}
	return -1;
}

static void *serve(void *arg)
{
	int fd = (int)(long)arg;
	int one = 1;

	setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
	while (read_request(fd) > 0) {
		if (work_us > 0) {
			struct timespec start, now;
			long elapsed;

			clock_gettime(CLOCK_MONOTONIC, &start);
			do {
				clock_gettime(CLOCK_MONOTONIC, &now);
				elapsed = (now.tv_sec - start.tv_sec) * 1000000L +
					  (now.tv_nsec - start.tv_nsec) / 1000L;
			} while (elapsed < work_us);
		}
		if (send_all(fd, header, header_len) < 0)
			break;
		if (send_all(fd, body, body_len) < 0)
			break;
	}
	close(fd);
	return NULL;
}

int main(int argc, char **argv)
{
	int port = 80;
	int listen_fd;
	struct sockaddr_in address;

	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--port") && i + 1 < argc)
			port = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--body-bytes") && i + 1 < argc)
			body_len = (size_t)atoi(argv[++i]);
		else if (!strcmp(argv[i], "--work-us") && i + 1 < argc)
			work_us = atol(argv[++i]);
	}
	if (body_len == 0 || body_len > MAX_BODY) {
		fprintf(stderr, "body size must be 1..%d\n", MAX_BODY);
		return 2;
	}
	memset(body, 'x', body_len);
	body[body_len - 1] = '\n';
	header_len = (size_t)snprintf(
		header, sizeof(header),
		"HTTP/1.1 200 OK\r\n"
		"Content-Type: text/plain\r\n"
		"Content-Length: %zu\r\n"
		"Connection: keep-alive\r\n"
		"\r\n",
		body_len);

	signal(SIGPIPE, SIG_IGN);

	listen_fd = socket(AF_INET, SOCK_STREAM, 0);
	if (listen_fd < 0) {
		perror("socket");
		return 1;
	}
	int one = 1;
	setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

	memset(&address, 0, sizeof(address));
	address.sin_family = AF_INET;
	address.sin_addr.s_addr = htonl(INADDR_ANY);
	address.sin_port = htons((uint16_t)port);
	if (bind(listen_fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
		perror("bind");
		return 1;
	}
	if (listen(listen_fd, 512) < 0) {
		perror("listen");
		return 1;
	}

	for (;;) {
		int client = accept(listen_fd, NULL, NULL);
		pthread_t thread;

		if (client < 0) {
			if (errno == EINTR)
				continue;
			perror("accept");
			return 1;
		}
		if (pthread_create(&thread, NULL, serve, (void *)(long)client) != 0) {
			close(client);
			continue;
		}
		pthread_detach(thread);
	}
}
