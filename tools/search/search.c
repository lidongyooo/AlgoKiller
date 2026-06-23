#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef MAP_ANONYMOUS
#define MAP_ANONYMOUS MAP_ANON
#endif

typedef struct {
    int fd;
    const unsigned char *data;
    size_t size;
} MappedFile;

typedef struct {
    unsigned char *pattern;
    size_t pattern_len;
    unsigned char lower[256];
    size_t skip[256];
} BmhSearcher;

typedef struct {
    const unsigned char *start;
    size_t len;
} LineView;

typedef struct {
    uint64_t lo;
    uint64_t hi;
} AsciiBits;

typedef struct {
    size_t *offsets;
    AsciiBits *bits;
    uint64_t count;
    uint64_t capacity;
} LineIndex;

typedef struct SearchThreadPool SearchThreadPool;

typedef struct {
    MappedFile mapped;
    LineIndex index;
    SearchThreadPool *search_pool;
} IndexedFile;

typedef struct {
    uint64_t line_no;
    uint64_t byte_offset;
} MatchResult;

typedef struct {
    const IndexedFile *indexed;
    const BmhSearcher *searcher;
    AsciiBits query_bits;
    atomic_uint_fast64_t cutoff_line;
    bool use_bit_filter;
    uint64_t limit;
    bool backward;
} SearchJob;

typedef struct {
    pthread_t thread;
    SearchThreadPool *pool;
    uint32_t id;
    uint64_t first_line;
    uint64_t last_line;
    MatchResult *results;
    uint64_t result_count;
    uint64_t result_capacity;
    int error;
} SearchWorker;

struct SearchThreadPool {
    pthread_mutex_t mutex;
    pthread_cond_t start_cond;
    pthread_cond_t done_cond;
    bool stop;
    uint64_t generation;
    uint32_t thread_count;
    uint32_t active_workers;
    SearchJob *job;
    SearchWorker *workers;
};

static LineView line_at_offset(const unsigned char *data, size_t size, size_t offset);
static const unsigned char *bmh_find(const BmhSearcher *searcher,
                                     const unsigned char *haystack,
                                     size_t haystack_len);
static uint32_t default_search_thread_count(void);
static SearchThreadPool *search_pool_create(uint32_t thread_count);
static void search_pool_destroy(SearchThreadPool *pool);

static void usage(FILE *stream) {
    fprintf(stream,
            "Usage:\n"
            "  ak_search match --file PATH --query TEXT [--from-line N | --before-line N] [--limit N]\n"
            "  ak_search context --file PATH --line N [--context N]\n"
            "  ak_search context --file PATH --line N [--before N] [--after N]\n"
            "  ak_search daemon --file PATH\n"
            "  ak_search selftest\n"
            "\n"
            "Match mode is ASCII case-insensitive. --before-line searches backward, nearest first.\n"
            "Output: one JSON object per line with 1-based line numbers.\n");
}

static bool parse_u64(const char *text, uint64_t *out) {
    char *end = NULL;
    errno = 0;
    unsigned long long value = strtoull(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0') {
        return false;
    }
    *out = (uint64_t)value;
    return true;
}

static int map_file(const char *path, MappedFile *mapped) {
    memset(mapped, 0, sizeof(*mapped));
    mapped->fd = -1;

    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        fprintf(stderr, "open failed: %s: %s\n", path, strerror(errno));
        return 1;
    }

    struct stat st;
    if (fstat(fd, &st) != 0) {
        fprintf(stderr, "fstat failed: %s: %s\n", path, strerror(errno));
        close(fd);
        return 1;
    }
    if (!S_ISREG(st.st_mode)) {
        fprintf(stderr, "not a regular file: %s\n", path);
        close(fd);
        return 1;
    }
    if (st.st_size < 0) {
        fprintf(stderr, "invalid file size: %s\n", path);
        close(fd);
        return 1;
    }

    mapped->fd = fd;
    mapped->size = (size_t)st.st_size;
    if (mapped->size == 0) {
        mapped->data = NULL;
        return 0;
    }

    void *ptr = mmap(NULL, mapped->size, PROT_READ, MAP_PRIVATE, fd, 0);
    if (ptr == MAP_FAILED) {
        fprintf(stderr, "mmap failed: %s: %s\n", path, strerror(errno));
        close(fd);
        mapped->fd = -1;
        return 1;
    }
    mapped->data = (const unsigned char *)ptr;
    return 0;
}

static void unmap_file(MappedFile *mapped) {
    if (mapped->data != NULL && mapped->size > 0) {
        munmap((void *)mapped->data, mapped->size);
    }
    if (mapped->fd >= 0) {
        close(mapped->fd);
    }
    memset(mapped, 0, sizeof(*mapped));
    mapped->fd = -1;
}

static void line_index_destroy(LineIndex *index) {
    if (index->offsets != NULL && index->capacity > 0) {
        size_t bytes = (size_t)index->capacity * sizeof(*index->offsets);
        munmap(index->offsets, bytes);
    }
    if (index->bits != NULL && index->capacity > 0) {
        size_t bytes = (size_t)index->capacity * sizeof(*index->bits);
        munmap(index->bits, bytes);
    }
    memset(index, 0, sizeof(*index));
}

static inline unsigned char fold_ascii_byte(unsigned char c) {
    if (c >= 'A' && c <= 'Z') {
        return (unsigned char)(c + ('a' - 'A'));
    }
    return c;
}

static inline AsciiBits ascii_bits_empty(void) {
    AsciiBits bits;
    bits.lo = 0;
    bits.hi = 0;
    return bits;
}

static inline void ascii_bits_add(AsciiBits *bits, unsigned char c) {
    if (c < 64) {
        bits->lo |= UINT64_C(1) << c;
    } else if (c < 128) {
        bits->hi |= UINT64_C(1) << (c - 64);
    }
}

static AsciiBits ascii_bits_for_text(const unsigned char *text, size_t len) {
    AsciiBits bits = ascii_bits_empty();
    for (size_t i = 0; i < len; i++) {
        unsigned char c = fold_ascii_byte(text[i]);
        ascii_bits_add(&bits, c);
    }
    return bits;
}

static AsciiBits ascii_bits_for_query(const char *query, bool *complete_out) {
    AsciiBits bits = ascii_bits_empty();
    bool complete = true;
    for (const unsigned char *cursor = (const unsigned char *)query; *cursor != '\0'; cursor++) {
        unsigned char c = fold_ascii_byte(*cursor);
        if (c >= 128) {
            complete = false;
            continue;
        }
        ascii_bits_add(&bits, c);
    }
    *complete_out = complete;
    return bits;
}

static inline bool ascii_bits_may_contain(AsciiBits line_bits, AsciiBits query_bits) {
#if defined(__aarch64__)
    uint64_t miss_lo;
    uint64_t miss_hi;
    __asm__ volatile (
        "bic %0, %2, %4\n\t"
        "bic %1, %3, %5\n\t"
        "orr %0, %0, %1\n\t"
        : "=&r"(miss_lo), "=&r"(miss_hi)
        : "r"(query_bits.lo), "r"(query_bits.hi), "r"(line_bits.lo), "r"(line_bits.hi)
    );
    return miss_lo == 0;
#elif defined(__x86_64__) && defined(__BMI__)
    uint64_t miss_lo = query_bits.lo;
    uint64_t miss_hi = query_bits.hi;
    __asm__ volatile (
        "andnq %2, %0, %0\n\t"
        "andnq %3, %1, %1\n\t"
        "orq %1, %0\n\t"
        : "+r"(miss_lo), "+r"(miss_hi)
        : "r"(line_bits.lo), "r"(line_bits.hi)
    );
    return miss_lo == 0;
#else
    return ((query_bits.lo & ~line_bits.lo) | (query_bits.hi & ~line_bits.hi)) == 0;
#endif
}

static LineView effective_search_line(LineView line) {
    if (line.len > 0 && line.start[line.len - 1] == '\r') {
        line.len--;
    }
    if (line.len > 0 && line.start[0] == '[') {
        const unsigned char *bang = memchr(line.start, '!', line.len);
        if (bang != NULL) {
            size_t prefix_len = (size_t)((bang + 1) - line.start);
            line.start += prefix_len;
            line.len -= prefix_len;
        }
    }
    return line;
}

static LineView search_line_for_index(const IndexedFile *indexed, uint64_t line_no) {
    size_t offset = indexed->index.offsets[line_no - 1];
    LineView line = line_at_offset(indexed->mapped.data, indexed->mapped.size, offset);
    return effective_search_line(line);
}

static uint64_t count_line_starts(const MappedFile *mapped) {
    if (mapped->size == 0) {
        return 0;
    }

    uint64_t count = 1;
    const unsigned char *cursor = mapped->data;
    const unsigned char *end = mapped->data + mapped->size;
    while (cursor < end) {
        const unsigned char *newline = memchr(cursor, '\n', (size_t)(end - cursor));
        if (newline == NULL) {
            break;
        }
        if (newline + 1 < end) {
            count++;
        }
        cursor = newline + 1;
    }
    return count;
}

static int line_index_reserve(LineIndex *index, uint64_t capacity) {
    if (capacity == 0) {
        return 0;
    }
    if (capacity > (uint64_t)(SIZE_MAX / sizeof(*index->offsets))) {
        fprintf(stderr, "line index too large\n");
        return 1;
    }

    size_t bytes = (size_t)capacity * sizeof(*index->offsets);
    void *ptr = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (ptr == MAP_FAILED) {
        fprintf(stderr, "mmap failed while preallocating line index: %s\n", strerror(errno));
        return 1;
    }
    index->offsets = (size_t *)ptr;
    index->capacity = capacity;

    if (capacity > (uint64_t)(SIZE_MAX / sizeof(*index->bits))) {
        fprintf(stderr, "line bitmap index too large\n");
        munmap(index->offsets, (size_t)index->capacity * sizeof(*index->offsets));
        index->offsets = NULL;
        index->capacity = 0;
        return 1;
    }
    bytes = (size_t)capacity * sizeof(*index->bits);
    ptr = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (ptr == MAP_FAILED) {
        fprintf(stderr, "mmap failed while preallocating line bitmap index: %s\n", strerror(errno));
        munmap(index->offsets, (size_t)index->capacity * sizeof(*index->offsets));
        index->offsets = NULL;
        index->capacity = 0;
        return 1;
    }
    index->bits = (AsciiBits *)ptr;
    return 0;
}

static int build_line_index(const MappedFile *mapped, LineIndex *index) {
    memset(index, 0, sizeof(*index));
    uint64_t line_count = count_line_starts(mapped);
    if (line_index_reserve(index, line_count) != 0) {
        return 1;
    }
    if (line_count == 0) {
        return 0;
    }

    const unsigned char *end = mapped->data + mapped->size;
    const unsigned char *line_start = mapped->data;
    while (line_start < end) {
        if (index->count >= index->capacity) {
            fprintf(stderr, "line index overflow while building index\n");
            return 1;
        }
        const unsigned char *newline = memchr(line_start, '\n', (size_t)(end - line_start));
        const unsigned char *line_end = newline == NULL ? end : newline;
        size_t offset = (size_t)(line_start - mapped->data);
        LineView line;
        line.start = line_start;
        line.len = (size_t)(line_end - line_start);
        LineView effective = effective_search_line(line);

        index->offsets[index->count] = offset;
        index->bits[index->count] = ascii_bits_for_text(effective.start, effective.len);
        index->count++;

        if (newline == NULL || newline + 1 >= end) {
            break;
        }
        line_start = newline + 1;
    }
    return 0;
}

static int indexed_file_open(const char *path, IndexedFile *indexed) {
    memset(indexed, 0, sizeof(*indexed));
    indexed->mapped.fd = -1;
    if (map_file(path, &indexed->mapped) != 0) {
        return 1;
    }
    if (build_line_index(&indexed->mapped, &indexed->index) != 0) {
        line_index_destroy(&indexed->index);
        unmap_file(&indexed->mapped);
        return 1;
    }
    indexed->search_pool = search_pool_create(default_search_thread_count());
    if (indexed->search_pool == NULL) {
        line_index_destroy(&indexed->index);
        unmap_file(&indexed->mapped);
        return 1;
    }
    return 0;
}

static void indexed_file_close(IndexedFile *indexed) {
    search_pool_destroy(indexed->search_pool);
    indexed->search_pool = NULL;
    line_index_destroy(&indexed->index);
    unmap_file(&indexed->mapped);
}

static bool indexed_line_start(const IndexedFile *indexed, uint64_t line_no, size_t *offset_out) {
    if (line_no == 0 || line_no > indexed->index.count) {
        return false;
    }
    *offset_out = indexed->index.offsets[line_no - 1];
    return true;
}

static uint32_t default_search_thread_count(void) {
    long cores = sysconf(_SC_NPROCESSORS_ONLN);
    if (cores < 1) {
        cores = 1;
    }
    long threads = cores / 2;
    if (threads < 1) {
        threads = 1;
    }
    if (threads > UINT32_MAX) {
        threads = UINT32_MAX;
    }
    return (uint32_t)threads;
}

static bool worker_append_match(SearchWorker *worker, uint64_t line_no, uint64_t byte_offset) {
    if (worker->result_count == worker->result_capacity) {
        uint64_t new_capacity = worker->result_capacity == 0 ? 16 : worker->result_capacity * 2;
        if (new_capacity < worker->result_capacity ||
            new_capacity > (uint64_t)(SIZE_MAX / sizeof(*worker->results))) {
            return false;
        }
        MatchResult *new_results = realloc(worker->results, (size_t)new_capacity * sizeof(*worker->results));
        if (new_results == NULL) {
            return false;
        }
        worker->results = new_results;
        worker->result_capacity = new_capacity;
    }
    worker->results[worker->result_count].line_no = line_no;
    worker->results[worker->result_count].byte_offset = byte_offset;
    worker->result_count++;
    return true;
}

static void worker_run_search(SearchWorker *worker, SearchJob *job) {
    worker->result_count = 0;
    worker->error = 0;
    if (worker->first_line == 0 || worker->last_line == 0 || worker->first_line > worker->last_line) {
        return;
    }

    const IndexedFile *indexed = job->indexed;
    if (job->backward) {
        for (uint64_t line_no = worker->last_line;
             line_no >= worker->first_line && worker->result_count < job->limit;
             line_no--) {
            uint64_t cutoff = atomic_load_explicit(&job->cutoff_line, memory_order_relaxed);
            if (cutoff != 0 && line_no < cutoff) {
                break;
            }
            if (!job->use_bit_filter ||
                ascii_bits_may_contain(indexed->index.bits[line_no - 1], job->query_bits)) {
                LineView line = search_line_for_index(indexed, line_no);
                if (bmh_find(job->searcher, line.start, line.len) != NULL) {
                    if (!worker_append_match(worker, line_no, (uint64_t)indexed->index.offsets[line_no - 1])) {
                        worker->error = 1;
                        return;
                    }
                    if (worker->result_count == job->limit) {
                        uint64_t current = atomic_load_explicit(&job->cutoff_line, memory_order_relaxed);
                        while (line_no > current &&
                               !atomic_compare_exchange_weak_explicit(
                                   &job->cutoff_line,
                                   &current,
                                   line_no,
                                   memory_order_relaxed,
                                   memory_order_relaxed)) {
                        }
                    }
                }
            }
            if (line_no == worker->first_line) {
                break;
            }
        }
        return;
    }

    for (uint64_t line_no = worker->first_line;
         line_no <= worker->last_line && worker->result_count < job->limit;
         line_no++) {
        uint64_t cutoff = atomic_load_explicit(&job->cutoff_line, memory_order_relaxed);
        if (cutoff != 0 && line_no > cutoff) {
            break;
        }
        if (!job->use_bit_filter ||
            ascii_bits_may_contain(indexed->index.bits[line_no - 1], job->query_bits)) {
            LineView line = search_line_for_index(indexed, line_no);
            if (bmh_find(job->searcher, line.start, line.len) != NULL) {
                if (!worker_append_match(worker, line_no, (uint64_t)indexed->index.offsets[line_no - 1])) {
                    worker->error = 1;
                    return;
                }
                if (worker->result_count == job->limit) {
                    uint64_t current = atomic_load_explicit(&job->cutoff_line, memory_order_relaxed);
                    while ((current == 0 || line_no < current) &&
                           !atomic_compare_exchange_weak_explicit(
                               &job->cutoff_line,
                               &current,
                               line_no,
                               memory_order_relaxed,
                               memory_order_relaxed)) {
                    }
                }
            }
        }
    }
}

static void *search_worker_main(void *arg) {
    SearchWorker *worker = (SearchWorker *)arg;
    SearchThreadPool *pool = worker->pool;
    uint64_t seen_generation = 0;

    pthread_mutex_lock(&pool->mutex);
    while (true) {
        while (!pool->stop && pool->generation == seen_generation) {
            pthread_cond_wait(&pool->start_cond, &pool->mutex);
        }
        if (pool->stop) {
            pthread_mutex_unlock(&pool->mutex);
            return NULL;
        }
        SearchJob *job = pool->job;
        seen_generation = pool->generation;
        pthread_mutex_unlock(&pool->mutex);

        worker_run_search(worker, job);

        pthread_mutex_lock(&pool->mutex);
        if (pool->active_workers > 0) {
            pool->active_workers--;
        }
        if (pool->active_workers == 0) {
            pthread_cond_signal(&pool->done_cond);
        }
    }
}

static SearchThreadPool *search_pool_create(uint32_t thread_count) {
    if (thread_count == 0) {
        thread_count = 1;
    }

    SearchThreadPool *pool = calloc(1, sizeof(*pool));
    if (pool == NULL) {
        fprintf(stderr, "calloc failed while creating search thread pool\n");
        return NULL;
    }
    pool->thread_count = thread_count;
    pool->workers = calloc(thread_count, sizeof(*pool->workers));
    if (pool->workers == NULL) {
        fprintf(stderr, "calloc failed while creating search workers\n");
        free(pool);
        return NULL;
    }
    int mutex_ready = 0;
    int start_cond_ready = 0;
    int done_cond_ready = 0;
    if (pthread_mutex_init(&pool->mutex, NULL) == 0) {
        mutex_ready = 1;
    }
    if (mutex_ready && pthread_cond_init(&pool->start_cond, NULL) == 0) {
        start_cond_ready = 1;
    }
    if (mutex_ready && start_cond_ready && pthread_cond_init(&pool->done_cond, NULL) == 0) {
        done_cond_ready = 1;
    }
    if (!mutex_ready || !start_cond_ready || !done_cond_ready) {
        fprintf(stderr, "pthread primitive init failed\n");
        if (done_cond_ready) {
            pthread_cond_destroy(&pool->done_cond);
        }
        if (start_cond_ready) {
            pthread_cond_destroy(&pool->start_cond);
        }
        if (mutex_ready) {
            pthread_mutex_destroy(&pool->mutex);
        }
        free(pool->workers);
        free(pool);
        return NULL;
    }

    for (uint32_t i = 0; i < thread_count; i++) {
        pool->workers[i].pool = pool;
        pool->workers[i].id = i;
        int err = pthread_create(&pool->workers[i].thread, NULL, search_worker_main, &pool->workers[i]);
        if (err != 0) {
            fprintf(stderr, "pthread_create failed: %s\n", strerror(err));
            pool->thread_count = i;
            search_pool_destroy(pool);
            return NULL;
        }
    }
    return pool;
}

static void search_pool_destroy(SearchThreadPool *pool) {
    if (pool == NULL) {
        return;
    }
    pthread_mutex_lock(&pool->mutex);
    pool->stop = true;
    pthread_cond_broadcast(&pool->start_cond);
    pthread_mutex_unlock(&pool->mutex);

    for (uint32_t i = 0; i < pool->thread_count; i++) {
        pthread_join(pool->workers[i].thread, NULL);
        free(pool->workers[i].results);
    }
    pthread_cond_destroy(&pool->done_cond);
    pthread_cond_destroy(&pool->start_cond);
    pthread_mutex_destroy(&pool->mutex);
    free(pool->workers);
    free(pool);
}

static int search_pool_run(SearchThreadPool *pool, SearchJob *job) {
    if (pool->thread_count == 0) {
        return 0;
    }
    pthread_mutex_lock(&pool->mutex);
    pool->job = job;
    pool->active_workers = pool->thread_count;
    pool->generation++;
    pthread_cond_broadcast(&pool->start_cond);
    while (pool->active_workers > 0) {
        pthread_cond_wait(&pool->done_cond, &pool->mutex);
    }
    pool->job = NULL;
    pthread_mutex_unlock(&pool->mutex);

    for (uint32_t i = 0; i < pool->thread_count; i++) {
        if (pool->workers[i].error != 0) {
            return 1;
        }
    }
    return 0;
}

static void init_ascii_lower(unsigned char lower[256]) {
    for (size_t i = 0; i < 256; i++) {
        lower[i] = (unsigned char)i;
    }
    for (unsigned char c = 'A'; c <= 'Z'; c++) {
        lower[c] = (unsigned char)(c + ('a' - 'A'));
    }
}

static int bmh_init(BmhSearcher *searcher, const char *query) {
    size_t len = strlen(query);
    memset(searcher, 0, sizeof(*searcher));
    init_ascii_lower(searcher->lower);

    searcher->pattern = malloc(len == 0 ? 1 : len);
    if (searcher->pattern == NULL) {
        fprintf(stderr, "malloc failed while preparing search pattern\n");
        return 1;
    }
    searcher->pattern_len = len;
    for (size_t i = 0; i < len; i++) {
        searcher->pattern[i] = searcher->lower[(unsigned char)query[i]];
    }

    for (size_t i = 0; i < 256; i++) {
        searcher->skip[i] = len == 0 ? 1 : len;
    }
    if (len > 1) {
        for (size_t i = 0; i + 1 < len; i++) {
            searcher->skip[searcher->pattern[i]] = len - 1 - i;
        }
    }
    return 0;
}

static void bmh_destroy(BmhSearcher *searcher) {
    free(searcher->pattern);
    memset(searcher, 0, sizeof(*searcher));
}

static bool folded_equal_at(const BmhSearcher *searcher,
                            const unsigned char *haystack,
                            size_t needle_len) {
    for (size_t i = 0; i < needle_len; i++) {
        if (searcher->lower[haystack[i]] != searcher->pattern[i]) {
            return false;
        }
    }
    return true;
}

static const unsigned char *bmh_find(const BmhSearcher *searcher,
                                     const unsigned char *haystack,
                                     size_t haystack_len) {
    size_t needle_len = searcher->pattern_len;
    if (needle_len == 0 || haystack_len < needle_len) {
        return NULL;
    }
    if (needle_len == 1) {
        for (size_t i = 0; i < haystack_len; i++) {
            if (searcher->lower[haystack[i]] == searcher->pattern[0]) {
                return haystack + i;
            }
        }
        return NULL;
    }

    size_t pos = 0;
    while (pos <= haystack_len - needle_len) {
        unsigned char last = searcher->lower[haystack[pos + needle_len - 1]];
        if (last == searcher->pattern[needle_len - 1] &&
            folded_equal_at(searcher, haystack + pos, needle_len)) {
            return haystack + pos;
        }
        pos += searcher->skip[last];
    }
    return NULL;
}

static LineView line_at_offset(const unsigned char *data, size_t size, size_t offset) {
    LineView line;
    line.start = data + offset;
    line.len = 0;

    size_t end = offset;
    while (end < size && data[end] != '\n') {
        end++;
    }
    line.len = end - offset;
    if (line.len > 0 && line.start[line.len - 1] == '\r') {
        line.len--;
    }
    return line;
}

static bool next_line_start(const MappedFile *mapped, size_t offset, size_t *next_offset_out) {
    if (offset >= mapped->size) {
        return false;
    }

    const unsigned char *start = mapped->data + offset;
    const unsigned char *end = mapped->data + mapped->size;
    const unsigned char *newline = memchr(start, '\n', (size_t)(end - start));
    if (newline == NULL || newline + 1 >= end) {
        return false;
    }

    *next_offset_out = (size_t)((newline + 1) - mapped->data);
    return true;
}

static bool prev_line_start(const MappedFile *mapped, size_t offset, size_t *prev_offset_out) {
    if (offset == 0 || mapped->size == 0) {
        return false;
    }

    size_t pos = offset - 1;
    while (pos > 0) {
        pos--;
        if (mapped->data[pos] == '\n') {
            *prev_offset_out = pos + 1;
            return true;
        }
    }

    *prev_offset_out = 0;
    return true;
}

static bool direct_line_start(const MappedFile *mapped,
                              uint64_t target_line,
                              uint64_t *line_no_out,
                              size_t *offset_out) {
    if (mapped->size == 0 || target_line == 0) {
        return false;
    }

    uint64_t line_no = 1;
    size_t offset = 0;
    while (line_no < target_line) {
        size_t next_offset = 0;
        if (!next_line_start(mapped, offset, &next_offset)) {
            return false;
        }
        offset = next_offset;
        line_no++;
    }

    *line_no_out = line_no;
    *offset_out = offset;
    return true;
}

static void direct_last_line_start(const MappedFile *mapped,
                                   uint64_t *line_no_out,
                                   size_t *offset_out) {
    uint64_t line_no = 1;
    size_t offset = 0;
    while (true) {
        size_t next_offset = 0;
        if (!next_line_start(mapped, offset, &next_offset)) {
            break;
        }
        offset = next_offset;
        line_no++;
    }

    *line_no_out = line_no;
    *offset_out = offset;
}

static void json_write_string(const unsigned char *text, size_t len) {
    putchar('"');
    for (size_t i = 0; i < len; i++) {
        unsigned char c = text[i];
        switch (c) {
            case '"':
                fputs("\\\"", stdout);
                break;
            case '\\':
                fputs("\\\\", stdout);
                break;
            case '\b':
                fputs("\\b", stdout);
                break;
            case '\f':
                fputs("\\f", stdout);
                break;
            case '\n':
                fputs("\\n", stdout);
                break;
            case '\r':
                fputs("\\r", stdout);
                break;
            case '\t':
                fputs("\\t", stdout);
                break;
            default:
                if (c < 0x20) {
                    printf("\\u%04x", c);
                } else {
                    putchar((int)c);
                }
                break;
        }
    }
    putchar('"');
}

static void json_write_cstr(const char *text) {
    json_write_string((const unsigned char *)text, strlen(text));
}

static void emit_line(const char *type,
                      uint64_t line_no,
                      uint64_t byte_offset,
                      bool is_target,
                      LineView line) {
    printf("{\"type\":\"%s\",\"line\":%" PRIu64 ",\"byte_offset\":%" PRIu64,
           type,
           line_no,
           byte_offset);
    if (is_target) {
        fputs(",\"target\":true", stdout);
    }
    fputs(",\"text\":", stdout);
    json_write_string(line.start, line.len);
    fputs("}\n", stdout);
}

static int emit_daemon_end(const char *status, const char *error) {
    fputs("{\"type\":\"daemon_end\",\"status\":", stdout);
    json_write_cstr(status);
    if (error != NULL && error[0] != '\0') {
        fputs(",\"error\":", stdout);
        json_write_cstr(error);
    }
    fputs("}\n", stdout);
    fflush(stdout);
    return strcmp(status, "ok") == 0 ? 0 : 1;
}

static int hex_value(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static char *hex_decode_to_cstr(const char *hex) {
    size_t hex_len = strlen(hex);
    if (hex_len % 2 != 0) {
        return NULL;
    }
    size_t out_len = hex_len / 2;
    char *out = malloc(out_len + 1);
    if (out == NULL) {
        return NULL;
    }
    for (size_t i = 0; i < out_len; i++) {
        int hi = hex_value(hex[i * 2]);
        int lo = hex_value(hex[i * 2 + 1]);
        if (hi < 0 || lo < 0) {
            free(out);
            return NULL;
        }
        out[i] = (char)((hi << 4) | lo);
    }
    out[out_len] = '\0';
    return out;
}

static int run_match_forward(const IndexedFile *indexed,
                             const char *query,
                             uint64_t from_line,
                             uint64_t limit) {
    if (limit == 0 || indexed->mapped.size == 0 || from_line > indexed->index.count) {
        return 0;
    }

    BmhSearcher searcher;
    if (bmh_init(&searcher, query) != 0) {
        return 1;
    }
    bool complete_query_bits = false;
    AsciiBits query_bits = ascii_bits_for_query(query, &complete_query_bits);

    uint64_t emitted = 0;
    for (uint64_t line_no = from_line; line_no <= indexed->index.count && emitted < limit; line_no++) {
        if (complete_query_bits &&
            !ascii_bits_may_contain(indexed->index.bits[line_no - 1], query_bits)) {
            continue;
        }
        size_t offset = indexed->index.offsets[line_no - 1];
        LineView effective = search_line_for_index(indexed, line_no);
        if (bmh_find(&searcher, effective.start, effective.len) != NULL) {
            LineView line = line_at_offset(indexed->mapped.data, indexed->mapped.size, offset);
            emit_line("match", line_no, (uint64_t)offset, false, line);
            emitted++;
        }
    }

    bmh_destroy(&searcher);
    return 0;
}

static int run_match_backward(const IndexedFile *indexed,
                              const char *query,
                              uint64_t before_line,
                              uint64_t limit) {
    if (limit == 0 || indexed->mapped.size == 0 || before_line <= 1) {
        return 0;
    }

    uint64_t line_no = before_line - 1;
    if (line_no > indexed->index.count) {
        line_no = indexed->index.count;
    }

    BmhSearcher searcher;
    if (bmh_init(&searcher, query) != 0) {
        return 1;
    }
    bool complete_query_bits = false;
    AsciiBits query_bits = ascii_bits_for_query(query, &complete_query_bits);

    uint64_t emitted = 0;
    while (line_no >= 1 && emitted < limit) {
        if (complete_query_bits &&
            !ascii_bits_may_contain(indexed->index.bits[line_no - 1], query_bits)) {
            if (line_no == 1) {
                break;
            }
            line_no--;
            continue;
        }
        size_t offset = indexed->index.offsets[line_no - 1];
        LineView effective = search_line_for_index(indexed, line_no);
        if (bmh_find(&searcher, effective.start, effective.len) != NULL) {
            LineView line = line_at_offset(indexed->mapped.data, indexed->mapped.size, offset);
            emit_line("match", line_no, (uint64_t)offset, false, line);
            emitted++;
        }
        if (line_no == 1) {
            break;
        }
        line_no--;
    }

    bmh_destroy(&searcher);
    return 0;
}

static int run_context(const IndexedFile *indexed,
                       uint64_t target_line,
                       uint64_t before,
                       uint64_t after) {
    if (indexed->mapped.size == 0) {
        return 0;
    }

    uint64_t first_line = target_line > before ? target_line - before : 1;
    uint64_t last_line = UINT64_MAX - target_line < after ? UINT64_MAX : target_line + after;
    if (last_line > indexed->index.count) {
        last_line = indexed->index.count;
    }

    size_t offset = 0;
    if (!indexed_line_start(indexed, first_line, &offset)) {
        return 0;
    }

    for (uint64_t line_no = first_line; line_no <= last_line; line_no++) {
        offset = indexed->index.offsets[line_no - 1];
        LineView line = line_at_offset(indexed->mapped.data, indexed->mapped.size, offset);
        emit_line("context", line_no, (uint64_t)offset, line_no == target_line, line);
    }
    return 0;
}

static void assign_forward_ranges(SearchThreadPool *pool, uint64_t first_line, uint64_t last_line) {
    uint64_t total = last_line - first_line + 1;
    uint64_t base = total / pool->thread_count;
    uint64_t extra = total % pool->thread_count;
    uint64_t cursor = first_line;

    for (uint32_t i = 0; i < pool->thread_count; i++) {
        uint64_t span = base + (i < extra ? 1 : 0);
        if (span == 0) {
            pool->workers[i].first_line = 0;
            pool->workers[i].last_line = 0;
            continue;
        }
        pool->workers[i].first_line = cursor;
        pool->workers[i].last_line = cursor + span - 1;
        cursor += span;
    }
}

static int emit_parallel_forward(SearchThreadPool *pool,
                                 const IndexedFile *indexed,
                                 uint64_t limit) {
    uint64_t emitted = 0;
    for (uint32_t i = 0; i < pool->thread_count && emitted < limit; i++) {
        SearchWorker *worker = &pool->workers[i];
        for (uint64_t j = 0; j < worker->result_count && emitted < limit; j++) {
            MatchResult result = worker->results[j];
            LineView line = line_at_offset(indexed->mapped.data, indexed->mapped.size, (size_t)result.byte_offset);
            emit_line("match", result.line_no, result.byte_offset, false, line);
            emitted++;
        }
    }
    return 0;
}

static int emit_parallel_backward(SearchThreadPool *pool,
                                  const IndexedFile *indexed,
                                  uint64_t limit) {
    uint64_t emitted = 0;
    uint32_t *positions = calloc(pool->thread_count, sizeof(*positions));
    if (positions == NULL) {
        fprintf(stderr, "calloc failed while merging backward search results\n");
        return 1;
    }

    while (emitted < limit) {
        uint32_t best_worker = UINT32_MAX;
        uint64_t best_line = 0;
        for (uint32_t i = 0; i < pool->thread_count; i++) {
            SearchWorker *worker = &pool->workers[i];
            if ((uint64_t)positions[i] >= worker->result_count) {
                continue;
            }
            uint64_t candidate = worker->results[positions[i]].line_no;
            if (best_worker == UINT32_MAX || candidate > best_line) {
                best_worker = i;
                best_line = candidate;
            }
        }
        if (best_worker == UINT32_MAX) {
            break;
        }
        SearchWorker *worker = &pool->workers[best_worker];
        MatchResult result = worker->results[positions[best_worker]];
        positions[best_worker]++;
        LineView line = line_at_offset(indexed->mapped.data, indexed->mapped.size, (size_t)result.byte_offset);
        emit_line("match", result.line_no, result.byte_offset, false, line);
        emitted++;
    }

    free(positions);
    return 0;
}

static int run_match_forward_parallel(const IndexedFile *indexed,
                                      const char *query,
                                      uint64_t from_line,
                                      uint64_t limit) {
    if (limit == 0 || indexed->mapped.size == 0 || from_line > indexed->index.count) {
        return 0;
    }

    BmhSearcher searcher;
    if (bmh_init(&searcher, query) != 0) {
        return 1;
    }
    bool complete_query_bits = false;
    AsciiBits query_bits = ascii_bits_for_query(query, &complete_query_bits);
    SearchThreadPool *pool = indexed->search_pool;
    assign_forward_ranges(pool, from_line, indexed->index.count);

    SearchJob job;
    job.indexed = indexed;
    job.searcher = &searcher;
    job.query_bits = query_bits;
    atomic_init(&job.cutoff_line, 0);
    job.use_bit_filter = complete_query_bits;
    job.limit = limit;
    job.backward = false;

    int result = search_pool_run(pool, &job);
    if (result == 0) {
        result = emit_parallel_forward(pool, indexed, limit);
    }
    bmh_destroy(&searcher);
    return result;
}

static int run_match_backward_parallel(const IndexedFile *indexed,
                                       const char *query,
                                       uint64_t before_line,
                                       uint64_t limit) {
    if (limit == 0 || indexed->mapped.size == 0 || before_line <= 1) {
        return 0;
    }

    uint64_t last_line = before_line - 1;
    if (last_line > indexed->index.count) {
        last_line = indexed->index.count;
    }
    if (last_line == 0) {
        return 0;
    }

    BmhSearcher searcher;
    if (bmh_init(&searcher, query) != 0) {
        return 1;
    }
    bool complete_query_bits = false;
    AsciiBits query_bits = ascii_bits_for_query(query, &complete_query_bits);
    SearchThreadPool *pool = indexed->search_pool;
    assign_forward_ranges(pool, 1, last_line);

    SearchJob job;
    job.indexed = indexed;
    job.searcher = &searcher;
    job.query_bits = query_bits;
    atomic_init(&job.cutoff_line, 0);
    job.use_bit_filter = complete_query_bits;
    job.limit = limit;
    job.backward = true;

    int result = search_pool_run(pool, &job);
    if (result == 0) {
        result = emit_parallel_backward(pool, indexed, limit);
    }
    bmh_destroy(&searcher);
    return result;
}

static int run_match_forward_direct(const MappedFile *mapped,
                                    const char *query,
                                    uint64_t from_line,
                                    uint64_t limit) {
    if (limit == 0 || mapped->size == 0) {
        return 0;
    }

    uint64_t line_no = 0;
    size_t offset = 0;
    if (!direct_line_start(mapped, from_line, &line_no, &offset)) {
        return 0;
    }

    BmhSearcher searcher;
    if (bmh_init(&searcher, query) != 0) {
        return 1;
    }
    bool complete_query_bits = false;
    AsciiBits query_bits = ascii_bits_for_query(query, &complete_query_bits);

    uint64_t emitted = 0;
    while (emitted < limit) {
        LineView line = line_at_offset(mapped->data, mapped->size, offset);
        LineView effective = effective_search_line(line);
        if ((!complete_query_bits ||
             ascii_bits_may_contain(ascii_bits_for_text(effective.start, effective.len), query_bits)) &&
            bmh_find(&searcher, effective.start, effective.len) != NULL) {
            emit_line("match", line_no, (uint64_t)offset, false, line);
            emitted++;
        }

        size_t next_offset = 0;
        if (!next_line_start(mapped, offset, &next_offset)) {
            break;
        }
        offset = next_offset;
        line_no++;
    }

    bmh_destroy(&searcher);
    return 0;
}

static int run_match_backward_direct(const MappedFile *mapped,
                                     const char *query,
                                     uint64_t before_line,
                                     uint64_t limit) {
    if (limit == 0 || mapped->size == 0 || before_line <= 1) {
        return 0;
    }

    uint64_t line_no = 0;
    size_t offset = 0;
    uint64_t anchor_line = 0;
    size_t anchor_offset = 0;
    if (direct_line_start(mapped, before_line, &anchor_line, &anchor_offset)) {
        if (!prev_line_start(mapped, anchor_offset, &offset)) {
            return 0;
        }
        line_no = anchor_line - 1;
    } else {
        direct_last_line_start(mapped, &line_no, &offset);
    }

    BmhSearcher searcher;
    if (bmh_init(&searcher, query) != 0) {
        return 1;
    }
    bool complete_query_bits = false;
    AsciiBits query_bits = ascii_bits_for_query(query, &complete_query_bits);

    uint64_t emitted = 0;
    while (line_no >= 1 && emitted < limit) {
        LineView line = line_at_offset(mapped->data, mapped->size, offset);
        LineView effective = effective_search_line(line);
        if ((!complete_query_bits ||
             ascii_bits_may_contain(ascii_bits_for_text(effective.start, effective.len), query_bits)) &&
            bmh_find(&searcher, effective.start, effective.len) != NULL) {
            emit_line("match", line_no, (uint64_t)offset, false, line);
            emitted++;
        }
        if (line_no == 1) {
            break;
        }

        size_t prev_offset = 0;
        if (!prev_line_start(mapped, offset, &prev_offset)) {
            break;
        }
        offset = prev_offset;
        line_no--;
    }

    bmh_destroy(&searcher);
    return 0;
}

static int run_context_direct(const MappedFile *mapped,
                              uint64_t target_line,
                              uint64_t before,
                              uint64_t after) {
    if (mapped->size == 0) {
        return 0;
    }

    uint64_t first_line = target_line > before ? target_line - before : 1;
    uint64_t last_line = UINT64_MAX - target_line < after ? UINT64_MAX : target_line + after;

    uint64_t line_no = 0;
    size_t offset = 0;
    if (!direct_line_start(mapped, first_line, &line_no, &offset)) {
        return 0;
    }

    while (line_no <= last_line) {
        LineView line = line_at_offset(mapped->data, mapped->size, offset);
        emit_line("context", line_no, (uint64_t)offset, line_no == target_line, line);

        size_t next_offset = 0;
        if (!next_line_start(mapped, offset, &next_offset)) {
            break;
        }
        offset = next_offset;
        line_no++;
    }
    return 0;
}

static int cmd_match(int argc, char **argv) {
    const char *path = NULL;
    const char *query = NULL;
    uint64_t from_line = 1;
    uint64_t before_line = 0;
    uint64_t limit = 20;
    bool has_from_line = false;
    bool has_before_line = false;

    for (int i = 2; i < argc; i++) {
        if (strcmp(argv[i], "--file") == 0 && i + 1 < argc) {
            path = argv[++i];
        } else if (strcmp(argv[i], "--query") == 0 && i + 1 < argc) {
            query = argv[++i];
        } else if (strcmp(argv[i], "--from-line") == 0 && i + 1 < argc) {
            if (!parse_u64(argv[++i], &from_line) || from_line == 0) {
                fprintf(stderr, "invalid --from-line\n");
                return 2;
            }
            has_from_line = true;
        } else if (strcmp(argv[i], "--before-line") == 0 && i + 1 < argc) {
            if (!parse_u64(argv[++i], &before_line) || before_line == 0) {
                fprintf(stderr, "invalid --before-line\n");
                return 2;
            }
            has_before_line = true;
        } else if (strcmp(argv[i], "--limit") == 0 && i + 1 < argc) {
            if (!parse_u64(argv[++i], &limit)) {
                fprintf(stderr, "invalid --limit\n");
                return 2;
            }
        } else {
            usage(stderr);
            return 2;
        }
    }

    if (path == NULL || query == NULL || query[0] == '\0') {
        usage(stderr);
        return 2;
    }
    if (has_from_line && has_before_line) {
        fprintf(stderr, "--from-line and --before-line are mutually exclusive\n");
        return 2;
    }
    if (limit == 0) {
        return 0;
    }

    MappedFile mapped;
    if (map_file(path, &mapped) != 0) {
        return 1;
    }

    int result = has_before_line
        ? run_match_backward_direct(&mapped, query, before_line, limit)
        : run_match_forward_direct(&mapped, query, from_line, limit);
    unmap_file(&mapped);
    return result;
}

static int cmd_context(int argc, char **argv) {
    const char *path = NULL;
    uint64_t target_line = 0;
    uint64_t before = 0;
    uint64_t after = 0;

    for (int i = 2; i < argc; i++) {
        if (strcmp(argv[i], "--file") == 0 && i + 1 < argc) {
            path = argv[++i];
        } else if (strcmp(argv[i], "--line") == 0 && i + 1 < argc) {
            if (!parse_u64(argv[++i], &target_line) || target_line == 0) {
                fprintf(stderr, "invalid --line\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--context") == 0 && i + 1 < argc) {
            uint64_t context = 0;
            if (!parse_u64(argv[++i], &context)) {
                fprintf(stderr, "invalid --context\n");
                return 2;
            }
            before = context;
            after = context;
        } else if (strcmp(argv[i], "--before") == 0 && i + 1 < argc) {
            if (!parse_u64(argv[++i], &before)) {
                fprintf(stderr, "invalid --before\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--after") == 0 && i + 1 < argc) {
            if (!parse_u64(argv[++i], &after)) {
                fprintf(stderr, "invalid --after\n");
                return 2;
            }
        } else {
            usage(stderr);
            return 2;
        }
    }

    if (path == NULL || target_line == 0) {
        usage(stderr);
        return 2;
    }

    MappedFile mapped;
    if (map_file(path, &mapped) != 0) {
        return 1;
    }

    int result = run_context_direct(&mapped, target_line, before, after);
    unmap_file(&mapped);
    return result;
}

static int handle_daemon_match(const IndexedFile *indexed, char **parts, int count) {
    if (count != 5) {
        return emit_daemon_end("error", "invalid match command");
    }

    uint64_t from_line = 0;
    uint64_t before_line = 0;
    uint64_t limit = 0;
    if (!parse_u64(parts[1], &from_line) ||
        !parse_u64(parts[2], &before_line) ||
        !parse_u64(parts[3], &limit)) {
        return emit_daemon_end("error", "invalid numeric match argument");
    }
    if ((from_line == 0 && before_line == 0) || (from_line != 0 && before_line != 0)) {
        return emit_daemon_end("error", "match requires exactly one of from_line or before_line");
    }

    char *query = hex_decode_to_cstr(parts[4]);
    if (query == NULL || query[0] == '\0') {
        free(query);
        return emit_daemon_end("error", "invalid or empty query");
    }

    int result = 0;
    if (indexed->search_pool != NULL && indexed->search_pool->thread_count > 1) {
        result = before_line != 0
            ? run_match_backward_parallel(indexed, query, before_line, limit)
            : run_match_forward_parallel(indexed, query, from_line, limit);
    } else {
        result = before_line != 0
            ? run_match_backward(indexed, query, before_line, limit)
            : run_match_forward(indexed, query, from_line, limit);
    }
    free(query);
    if (result != 0) {
        return emit_daemon_end("error", "match failed");
    }
    return emit_daemon_end("ok", NULL);
}

static int handle_daemon_context(const IndexedFile *indexed, char **parts, int count) {
    if (count != 4) {
        return emit_daemon_end("error", "invalid context command");
    }

    uint64_t line = 0;
    uint64_t before = 0;
    uint64_t after = 0;
    if (!parse_u64(parts[1], &line) ||
        !parse_u64(parts[2], &before) ||
        !parse_u64(parts[3], &after) ||
        line == 0) {
        return emit_daemon_end("error", "invalid numeric context argument");
    }

    int result = run_context(indexed, line, before, after);
    if (result != 0) {
        return emit_daemon_end("error", "context failed");
    }
    return emit_daemon_end("ok", NULL);
}

static int cmd_daemon(int argc, char **argv) {
    const char *path = NULL;

    for (int i = 2; i < argc; i++) {
        if (strcmp(argv[i], "--file") == 0 && i + 1 < argc) {
            path = argv[++i];
        } else {
            usage(stderr);
            return 2;
        }
    }

    if (path == NULL) {
        usage(stderr);
        return 2;
    }

    IndexedFile indexed;
    if (indexed_file_open(path, &indexed) != 0) {
        return 1;
    }

    printf("{\"type\":\"daemon_ready\",\"status\":\"ok\",\"line_count\":%" PRIu64
           ",\"search_threads\":%" PRIu32 "}\n",
           indexed.index.count,
           indexed.search_pool == NULL ? 0 : indexed.search_pool->thread_count);
    fflush(stdout);

    char command[65536];
    while (fgets(command, sizeof(command), stdin) != NULL) {
        size_t len = strlen(command);
        while (len > 0 && (command[len - 1] == '\n' || command[len - 1] == '\r')) {
            command[--len] = '\0';
        }
        if (len == 0) {
            continue;
        }
        if (strcmp(command, "quit") == 0) {
            break;
        }

        char *parts[6] = {0};
        int count = 0;
        char *saveptr = NULL;
        char *token = strtok_r(command, "\t", &saveptr);
        while (token != NULL && count < 6) {
            parts[count++] = token;
            token = strtok_r(NULL, "\t", &saveptr);
        }
        if (token != NULL) {
            emit_daemon_end("error", "too many command fields");
            continue;
        }

        if (count > 0 && strcmp(parts[0], "match") == 0) {
            handle_daemon_match(&indexed, parts, count);
        } else if (count > 0 && strcmp(parts[0], "context") == 0) {
            handle_daemon_context(&indexed, parts, count);
        } else {
            emit_daemon_end("error", "unknown daemon command");
        }
    }

    indexed_file_close(&indexed);
    return 0;
}

static int cmd_selftest(void) {
    AsciiBits line = ascii_bits_empty();
    AsciiBits query = ascii_bits_empty();
    ascii_bits_add(&line, 'a');
    ascii_bits_add(&line, 'b');
    ascii_bits_add(&line, 'z');
    ascii_bits_add(&query, 'a');
    ascii_bits_add(&query, 'z');
    if (!ascii_bits_may_contain(line, query)) {
        fprintf(stderr, "ascii_bits_may_contain false negative\n");
        return 1;
    }
    ascii_bits_add(&query, 'x');
    if (ascii_bits_may_contain(line, query)) {
        fprintf(stderr, "ascii_bits_may_contain false positive in selftest\n");
        return 1;
    }

    LineView line_view;
    line_view.start = (const unsigned char *)"[lib] 0x100!0x200 target";
    line_view.len = strlen((const char *)line_view.start);
    LineView effective = effective_search_line(line_view);
    if (effective.len != strlen("0x200 target") ||
        memcmp(effective.start, "0x200 target", effective.len) != 0) {
        fprintf(stderr, "effective_search_line failed\n");
        return 1;
    }

    printf("{\"type\":\"selftest\",\"status\":\"ok\",\"search_threads\":%" PRIu32 "}\n",
           default_search_thread_count());
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        usage(stderr);
        return 2;
    }
    if (strcmp(argv[1], "-h") == 0 || strcmp(argv[1], "--help") == 0) {
        usage(stdout);
        return 0;
    }
    if (strcmp(argv[1], "match") == 0) {
        return cmd_match(argc, argv);
    }
    if (strcmp(argv[1], "context") == 0) {
        return cmd_context(argc, argv);
    }
    if (strcmp(argv[1], "daemon") == 0) {
        return cmd_daemon(argc, argv);
    }
    if (strcmp(argv[1], "selftest") == 0) {
        return cmd_selftest();
    }
    usage(stderr);
    return 2;
}
