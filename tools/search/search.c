#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
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
    size_t **offset_blocks;
    AsciiBits **bit_blocks;
    uint64_t count;
    uint64_t block_count;
    uint64_t block_capacity;
} LineIndex;

typedef struct {
    uint32_t id;
    char *path;
    char *name;
    MappedFile mapped;
    LineIndex index;
} IndexedFile;

typedef struct {
    IndexedFile *files;
    uint32_t count;
    uint32_t capacity;
} TraceStore;

typedef struct {
    uint32_t file_id;
    uint64_t line_no;
    uint64_t byte_offset;
} MatchResult;

typedef struct MultiFileThreadPool MultiFileThreadPool;
typedef struct FileCandidate FileCandidate;

typedef struct {
    pthread_t thread;
    MultiFileThreadPool *pool;
    uint32_t id;
    MatchResult *results;
    uint64_t result_count;
    uint64_t result_capacity;
    int error;
} SearchWorker;

typedef struct {
    const TraceStore *store;
    const BmhSearcher *searcher;
    AsciiBits query_bits;
    bool use_bit_filter;
    uint64_t from_line;
    uint64_t limit;
    uint32_t next_file_index;
    pthread_mutex_t queue_mutex;
} AllSearchJob;

typedef struct {
    TraceStore *store;
    FileCandidate *candidates;
    uint32_t count;
    uint32_t next_index;
    int error;
    pthread_mutex_t mutex;
} IndexBuildJob;

#define LINE_INDEX_BLOCK_SHIFT 16
#define LINE_INDEX_BLOCK_LINES (UINT64_C(1) << LINE_INDEX_BLOCK_SHIFT)
#define LINE_INDEX_BLOCK_MASK (LINE_INDEX_BLOCK_LINES - 1)

static AsciiBits g_ascii_byte_bits[256];
static pthread_once_t g_ascii_byte_bits_once = PTHREAD_ONCE_INIT;

struct MultiFileThreadPool {
    pthread_mutex_t mutex;
    pthread_cond_t start_cond;
    pthread_cond_t done_cond;
    bool stop;
    uint64_t generation;
    uint32_t thread_count;
    uint32_t active_workers;
    AllSearchJob *job;
    SearchWorker *workers;
};

static LineView line_at_offset(const unsigned char *data, size_t size, size_t offset);
static const unsigned char *bmh_find(const BmhSearcher *searcher,
                                     const unsigned char *haystack,
                                     size_t haystack_len);
static void search_pool_destroy(MultiFileThreadPool *pool);
static uint32_t default_search_thread_count(void);

static void usage(FILE *stream) {
    fprintf(stream,
            "Usage:\n"
            "  ak_search daemon --dir PATH\n"
            "  ak_search daemon PATH\n"
            "  ak_search selftest\n"
            "\n"
            "Daemon protocol:\n"
            "  list\n"
            "  match FILE_ID FROM_LINE BEFORE_LINE LIMIT QUERY_HEX\n"
            "  trace_all_search LIMIT QUERY_HEX\n"
            "  context FILE_ID LINE BEFORE AFTER\n"
            "\n"
            "FILE_ID is assigned by descending .log file size, starting at 1.\n");
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

static bool parse_u32(const char *text, uint32_t *out) {
    uint64_t value = 0;
    if (!parse_u64(text, &value) || value == 0 || value > UINT32_MAX) {
        return false;
    }
    *out = (uint32_t)value;
    return true;
}

static bool has_log_suffix(const char *name) {
    size_t len = strlen(name);
    return len >= 4 && strcmp(name + len - 4, ".log") == 0;
}

static char *join_path(const char *dir, const char *name) {
    size_t dir_len = strlen(dir);
    size_t name_len = strlen(name);
    bool need_slash = dir_len > 0 && dir[dir_len - 1] != '/';
    size_t total = dir_len + (need_slash ? 1 : 0) + name_len + 1;
    char *out = malloc(total);
    if (out == NULL) {
        return NULL;
    }
    memcpy(out, dir, dir_len);
    size_t pos = dir_len;
    if (need_slash) {
        out[pos++] = '/';
    }
    memcpy(out + pos, name, name_len);
    out[pos + name_len] = '\0';
    return out;
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
    if (index->offset_blocks != NULL) {
        for (uint64_t i = 0; i < index->block_count; i++) {
            if (index->offset_blocks[i] != NULL) {
                munmap(index->offset_blocks[i], (size_t)LINE_INDEX_BLOCK_LINES * sizeof(**index->offset_blocks));
            }
        }
        free(index->offset_blocks);
    }
    if (index->bit_blocks != NULL) {
        for (uint64_t i = 0; i < index->block_count; i++) {
            if (index->bit_blocks[i] != NULL) {
                munmap(index->bit_blocks[i], (size_t)LINE_INDEX_BLOCK_LINES * sizeof(**index->bit_blocks));
            }
        }
        free(index->bit_blocks);
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

static inline AsciiBits ascii_bits_or(AsciiBits lhs, AsciiBits rhs) {
    lhs.lo |= rhs.lo;
    lhs.hi |= rhs.hi;
    return lhs;
}

static void init_ascii_byte_bits(void) {
    for (size_t i = 0; i < 256; i++) {
        g_ascii_byte_bits[i] = ascii_bits_empty();
        ascii_bits_add(&g_ascii_byte_bits[i], fold_ascii_byte((unsigned char)i));
    }
}

static const AsciiBits *ascii_byte_bit_table(void) {
    pthread_once(&g_ascii_byte_bits_once, init_ascii_byte_bits);
    return g_ascii_byte_bits;
}

static AsciiBits ascii_bits_for_text(const unsigned char *text, size_t len) {
    AsciiBits bits = ascii_bits_empty();
    const AsciiBits *table = ascii_byte_bit_table();
    for (size_t i = 0; i < len; i++) {
        bits = ascii_bits_or(bits, table[text[i]]);
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

static int line_index_grow_block_arrays(LineIndex *index) {
    if (index->block_count < index->block_capacity) {
        return 0;
    }
    uint64_t new_capacity = index->block_capacity == 0 ? 8 : index->block_capacity * 2;
    if (new_capacity < index->block_capacity ||
        new_capacity > (uint64_t)(SIZE_MAX / sizeof(*index->offset_blocks)) ||
        new_capacity > (uint64_t)(SIZE_MAX / sizeof(*index->bit_blocks))) {
        fprintf(stderr, "line index block table too large\n");
        return 1;
    }

    size_t offset_bytes = (size_t)new_capacity * sizeof(*index->offset_blocks);
    size_t bit_bytes = (size_t)new_capacity * sizeof(*index->bit_blocks);
    size_t old_offset_bytes = (size_t)index->block_capacity * sizeof(*index->offset_blocks);
    size_t old_bit_bytes = (size_t)index->block_capacity * sizeof(*index->bit_blocks);

    size_t **new_offsets = realloc(index->offset_blocks, offset_bytes);
    if (new_offsets == NULL) {
        return 1;
    }
    index->offset_blocks = new_offsets;
    memset((unsigned char *)index->offset_blocks + old_offset_bytes, 0, offset_bytes - old_offset_bytes);

    AsciiBits **new_bits = realloc(index->bit_blocks, bit_bytes);
    if (new_bits == NULL) {
        return 1;
    }
    index->bit_blocks = new_bits;
    memset((unsigned char *)index->bit_blocks + old_bit_bytes, 0, bit_bytes - old_bit_bytes);

    index->block_capacity = new_capacity;
    return 0;
}

static int line_index_add_block(LineIndex *index) {
    if (line_index_grow_block_arrays(index) != 0) {
        return 1;
    }

    size_t offset_bytes = (size_t)LINE_INDEX_BLOCK_LINES * sizeof(**index->offset_blocks);
    size_t bit_bytes = (size_t)LINE_INDEX_BLOCK_LINES * sizeof(**index->bit_blocks);
    void *offsets = mmap(NULL, offset_bytes, PROT_READ | PROT_WRITE,
                         MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (offsets == MAP_FAILED) {
        fprintf(stderr, "mmap failed while allocating line index block: %s\n", strerror(errno));
        return 1;
    }

    void *bits = mmap(NULL, bit_bytes, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (bits == MAP_FAILED) {
        fprintf(stderr, "mmap failed while allocating line bitmap block: %s\n", strerror(errno));
        munmap(offsets, offset_bytes);
        return 1;
    }

    index->offset_blocks[index->block_count] = (size_t *)offsets;
    index->bit_blocks[index->block_count] = (AsciiBits *)bits;
    index->block_count++;
    return 0;
}

static int line_index_append(LineIndex *index, size_t offset, AsciiBits bits) {
    uint64_t in_block = index->count & LINE_INDEX_BLOCK_MASK;
    if (in_block == 0 && line_index_add_block(index) != 0) {
        return 1;
    }
    uint64_t block = index->count >> LINE_INDEX_BLOCK_SHIFT;
    index->offset_blocks[block][in_block] = offset;
    index->bit_blocks[block][in_block] = bits;
    index->count++;
    return 0;
}

static inline size_t line_index_offset(const LineIndex *index, uint64_t line_no) {
    uint64_t pos = line_no - 1;
    return index->offset_blocks[pos >> LINE_INDEX_BLOCK_SHIFT][pos & LINE_INDEX_BLOCK_MASK];
}

static inline AsciiBits line_index_bits(const LineIndex *index, uint64_t line_no) {
    uint64_t pos = line_no - 1;
    return index->bit_blocks[pos >> LINE_INDEX_BLOCK_SHIFT][pos & LINE_INDEX_BLOCK_MASK];
}

static LineView search_line_for_index(const IndexedFile *file, uint64_t line_no) {
    size_t offset = line_index_offset(&file->index, line_no);
    LineView line = line_at_offset(file->mapped.data, file->mapped.size, offset);
    return effective_search_line(line);
}

static int build_line_index(const MappedFile *mapped, LineIndex *index) {
    memset(index, 0, sizeof(*index));
    if (mapped->size == 0) {
        return 0;
    }

    const unsigned char *end = mapped->data + mapped->size;
    const unsigned char *line_start = mapped->data;
    while (line_start < end) {
        const unsigned char *newline = memchr(line_start, '\n', (size_t)(end - line_start));
        const unsigned char *line_end = newline == NULL ? end : newline;
        LineView line;
        line.start = line_start;
        line.len = (size_t)(line_end - line_start);
        LineView effective = effective_search_line(line);
        size_t offset = (size_t)(line_start - mapped->data);

        if (line_index_append(index, offset, ascii_bits_for_text(effective.start, effective.len)) != 0) {
            return 1;
        }

        if (newline == NULL || newline + 1 >= end) {
            break;
        }
        line_start = newline + 1;
    }
    return 0;
}

static int indexed_file_open(const char *path, const char *name, IndexedFile *file) {
    memset(file, 0, sizeof(*file));
    file->mapped.fd = -1;
    file->path = strdup(path);
    file->name = strdup(name);
    if (file->path == NULL || file->name == NULL) {
        fprintf(stderr, "strdup failed while opening file metadata\n");
        return 1;
    }
    if (map_file(path, &file->mapped) != 0) {
        return 1;
    }
    if (build_line_index(&file->mapped, &file->index) != 0) {
        line_index_destroy(&file->index);
        unmap_file(&file->mapped);
        return 1;
    }
    return 0;
}

static void indexed_file_close(IndexedFile *file) {
    line_index_destroy(&file->index);
    unmap_file(&file->mapped);
    free(file->path);
    free(file->name);
    memset(file, 0, sizeof(*file));
}

static bool indexed_line_start(const IndexedFile *file, uint64_t line_no, size_t *offset_out) {
    if (line_no == 0 || line_no > file->index.count) {
        return false;
    }
    *offset_out = line_index_offset(&file->index, line_no);
    return true;
}

static void trace_store_destroy(TraceStore *store) {
    if (store->files != NULL) {
        for (uint32_t i = 0; i < store->count; i++) {
            indexed_file_close(&store->files[i]);
        }
        free(store->files);
    }
    memset(store, 0, sizeof(*store));
}

struct FileCandidate {
    char *path;
    char *name;
    size_t size;
};

static int compare_candidates_desc_size(const void *lhs, const void *rhs) {
    const FileCandidate *a = (const FileCandidate *)lhs;
    const FileCandidate *b = (const FileCandidate *)rhs;
    if (a->size < b->size) {
        return 1;
    }
    if (a->size > b->size) {
        return -1;
    }
    return strcmp(a->name, b->name);
}

static void free_candidates(FileCandidate *items, uint32_t count) {
    if (items == NULL) {
        return;
    }
    for (uint32_t i = 0; i < count; i++) {
        free(items[i].path);
        free(items[i].name);
    }
    free(items);
}

static int discover_log_files(const char *dir_path, FileCandidate **items_out, uint32_t *count_out) {
    DIR *dir = opendir(dir_path);
    if (dir == NULL) {
        fprintf(stderr, "opendir failed: %s: %s\n", dir_path, strerror(errno));
        return 1;
    }

    FileCandidate *items = NULL;
    uint32_t count = 0;
    uint32_t capacity = 0;
    struct dirent *entry = NULL;
    while ((entry = readdir(dir)) != NULL) {
        if (!has_log_suffix(entry->d_name)) {
            continue;
        }
        char *path = join_path(dir_path, entry->d_name);
        if (path == NULL) {
            closedir(dir);
            free_candidates(items, count);
            return 1;
        }
        struct stat st;
        if (stat(path, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size < 0) {
            free(path);
            continue;
        }
        if (count == capacity) {
            uint32_t new_capacity = capacity == 0 ? 8 : capacity * 2;
            FileCandidate *new_items = realloc(items, (size_t)new_capacity * sizeof(*items));
            if (new_items == NULL) {
                free(path);
                closedir(dir);
                free_candidates(items, count);
                return 1;
            }
            items = new_items;
            capacity = new_capacity;
        }
        items[count].path = path;
        items[count].name = strdup(entry->d_name);
        items[count].size = (size_t)st.st_size;
        if (items[count].name == NULL) {
            closedir(dir);
            free_candidates(items, count + 1);
            return 1;
        }
        count++;
    }
    closedir(dir);

    qsort(items, count, sizeof(*items), compare_candidates_desc_size);
    *items_out = items;
    *count_out = count;
    return 0;
}

static void *index_build_worker_main(void *arg) {
    IndexBuildJob *job = (IndexBuildJob *)arg;
    while (true) {
        pthread_mutex_lock(&job->mutex);
        if (job->error != 0 || job->next_index >= job->count) {
            pthread_mutex_unlock(&job->mutex);
            break;
        }
        uint32_t index = job->next_index++;
        pthread_mutex_unlock(&job->mutex);

        if (indexed_file_open(job->candidates[index].path,
                              job->candidates[index].name,
                              &job->store->files[index]) != 0) {
            pthread_mutex_lock(&job->mutex);
            job->error = 1;
            pthread_mutex_unlock(&job->mutex);
            break;
        }
        job->store->files[index].id = index + 1;
    }
    return NULL;
}

static int trace_store_build_indexes_parallel(TraceStore *store,
                                              FileCandidate *candidates,
                                              uint32_t count) {
    uint32_t thread_count = default_search_thread_count();
    if (thread_count > count) {
        thread_count = count;
    }
    if (thread_count < 1) {
        thread_count = 1;
    }

    IndexBuildJob job;
    memset(&job, 0, sizeof(job));
    job.store = store;
    job.candidates = candidates;
    job.count = count;
    if (pthread_mutex_init(&job.mutex, NULL) != 0) {
        return 1;
    }

    pthread_t *threads = calloc(thread_count, sizeof(*threads));
    if (threads == NULL) {
        pthread_mutex_destroy(&job.mutex);
        return 1;
    }

    uint32_t started = 0;
    for (; started < thread_count; started++) {
        int err = pthread_create(&threads[started], NULL, index_build_worker_main, &job);
        if (err != 0) {
            fprintf(stderr, "pthread_create failed while building indexes: %s\n", strerror(err));
            pthread_mutex_lock(&job.mutex);
            job.error = 1;
            pthread_mutex_unlock(&job.mutex);
            break;
        }
    }
    for (uint32_t i = 0; i < started; i++) {
        pthread_join(threads[i], NULL);
    }
    free(threads);
    pthread_mutex_destroy(&job.mutex);

    if (job.error != 0) {
        return 1;
    }
    store->count = count;
    return 0;
}

static int trace_store_open_dir(const char *dir_path, TraceStore *store) {
    memset(store, 0, sizeof(*store));
    FileCandidate *candidates = NULL;
    uint32_t count = 0;
    if (discover_log_files(dir_path, &candidates, &count) != 0) {
        return 1;
    }
    if (count == 0) {
        fprintf(stderr, "no .log files found in directory: %s\n", dir_path);
        free_candidates(candidates, count);
        return 1;
    }

    store->files = calloc(count, sizeof(*store->files));
    if (store->files == NULL) {
        free_candidates(candidates, count);
        return 1;
    }
    store->capacity = count;
    store->count = count;
    for (uint32_t i = 0; i < count; i++) {
        store->files[i].mapped.fd = -1;
    }
    if (trace_store_build_indexes_parallel(store, candidates, count) != 0) {
        free_candidates(candidates, count);
        trace_store_destroy(store);
        return 1;
    }
    free_candidates(candidates, count);
    return 0;
}

static IndexedFile *trace_store_get_file(const TraceStore *store, uint32_t file_id) {
    if (file_id == 0 || file_id > store->count) {
        return NULL;
    }
    return &((TraceStore *)store)->files[file_id - 1];
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
                      uint32_t file_id,
                      uint64_t line_no,
                      uint64_t byte_offset,
                      bool is_target,
                      LineView line) {
    printf("{\"type\":\"%s\",\"file_id\":%" PRIu32 ",\"line\":%" PRIu64
           ",\"byte_offset\":%" PRIu64,
           type,
           file_id,
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

static bool append_match(MatchResult **results,
                         uint64_t *count,
                         uint64_t *capacity,
                         uint32_t file_id,
                         uint64_t line_no,
                         uint64_t byte_offset) {
    if (*count == *capacity) {
        uint64_t new_capacity = *capacity == 0 ? 16 : *capacity * 2;
        if (new_capacity < *capacity || new_capacity > (uint64_t)(SIZE_MAX / sizeof(**results))) {
            return false;
        }
        MatchResult *new_results = realloc(*results, (size_t)new_capacity * sizeof(**results));
        if (new_results == NULL) {
            return false;
        }
        *results = new_results;
        *capacity = new_capacity;
    }
    (*results)[*count].file_id = file_id;
    (*results)[*count].line_no = line_no;
    (*results)[*count].byte_offset = byte_offset;
    (*count)++;
    return true;
}

static int collect_file_matches_forward(const IndexedFile *file,
                                        const BmhSearcher *searcher,
                                        AsciiBits query_bits,
                                        bool use_bit_filter,
                                        uint64_t from_line,
                                        uint64_t limit,
                                        MatchResult **results,
                                        uint64_t *count,
                                        uint64_t *capacity) {
    if (limit == 0 || file->mapped.size == 0 || from_line > file->index.count) {
        return 0;
    }

    uint64_t start_count = *count;
    for (uint64_t line_no = from_line;
         line_no <= file->index.count && (*count - start_count) < limit;
         line_no++) {
        if (use_bit_filter && !ascii_bits_may_contain(line_index_bits(&file->index, line_no), query_bits)) {
            continue;
        }
        LineView effective = search_line_for_index(file, line_no);
        if (bmh_find(searcher, effective.start, effective.len) != NULL) {
            size_t offset = line_index_offset(&file->index, line_no);
            if (!append_match(results, count, capacity, file->id, line_no, (uint64_t)offset)) {
                return 1;
            }
        }
    }
    return 0;
}

static int collect_file_matches_backward(const IndexedFile *file,
                                         const BmhSearcher *searcher,
                                         AsciiBits query_bits,
                                         bool use_bit_filter,
                                         uint64_t before_line,
                                         uint64_t limit,
                                         MatchResult **results,
                                         uint64_t *count,
                                         uint64_t *capacity) {
    if (limit == 0 || file->mapped.size == 0 || before_line <= 1) {
        return 0;
    }

    uint64_t line_no = before_line - 1;
    if (line_no > file->index.count) {
        line_no = file->index.count;
    }

    uint64_t start_count = *count;
    while (line_no >= 1 && (*count - start_count) < limit) {
        if (!use_bit_filter || ascii_bits_may_contain(line_index_bits(&file->index, line_no), query_bits)) {
            LineView effective = search_line_for_index(file, line_no);
            if (bmh_find(searcher, effective.start, effective.len) != NULL) {
                size_t offset = line_index_offset(&file->index, line_no);
                if (!append_match(results, count, capacity, file->id, line_no, (uint64_t)offset)) {
                    return 1;
                }
            }
        }
        if (line_no == 1) {
            break;
        }
        line_no--;
    }
    return 0;
}

static int emit_matches(const TraceStore *store, const MatchResult *results, uint64_t count) {
    for (uint64_t i = 0; i < count; i++) {
        const MatchResult *result = &results[i];
        IndexedFile *file = trace_store_get_file(store, result->file_id);
        if (file == NULL) {
            return 1;
        }
        LineView line = line_at_offset(file->mapped.data, file->mapped.size, (size_t)result->byte_offset);
        emit_line("match", result->file_id, result->line_no, result->byte_offset, false, line);
    }
    return 0;
}

static int run_match_single_file(const TraceStore *store,
                                 IndexedFile *file,
                                 const char *query,
                                 uint64_t from_line,
                                 uint64_t before_line,
                                 uint64_t limit) {
    BmhSearcher searcher;
    if (bmh_init(&searcher, query) != 0) {
        return 1;
    }
    bool complete_query_bits = false;
    AsciiBits query_bits = ascii_bits_for_query(query, &complete_query_bits);

    MatchResult *results = NULL;
    uint64_t count = 0;
    uint64_t capacity = 0;
    int result = before_line != 0
        ? collect_file_matches_backward(file, &searcher, query_bits, complete_query_bits,
                                        before_line, limit, &results, &count, &capacity)
        : collect_file_matches_forward(file, &searcher, query_bits, complete_query_bits,
                                       from_line, limit, &results, &count, &capacity);
    if (result == 0) {
        result = emit_matches(store, results, count);
    }
    free(results);
    bmh_destroy(&searcher);
    return result;
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

static void worker_clear_results(SearchWorker *worker) {
    worker->result_count = 0;
    worker->error = 0;
}

static void *search_worker_main(void *arg) {
    SearchWorker *worker = (SearchWorker *)arg;
    MultiFileThreadPool *pool = worker->pool;
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

        AllSearchJob *job = pool->job;
        seen_generation = pool->generation;
        pthread_mutex_unlock(&pool->mutex);

        worker_clear_results(worker);
        while (true) {
            pthread_mutex_lock(&job->queue_mutex);
            uint32_t file_index = job->next_file_index;
            if (file_index < job->store->count) {
                job->next_file_index++;
            }
            pthread_mutex_unlock(&job->queue_mutex);

            if (file_index >= job->store->count) {
                break;
            }

            const IndexedFile *file = &job->store->files[file_index];
            if (collect_file_matches_forward(file, job->searcher, job->query_bits, job->use_bit_filter,
                                             job->from_line, job->limit, &worker->results, &worker->result_count,
                                             &worker->result_capacity) != 0) {
                worker->error = 1;
                break;
            }
        }

        pthread_mutex_lock(&pool->mutex);
        if (pool->active_workers > 0) {
            pool->active_workers--;
        }
        if (pool->active_workers == 0) {
            pthread_cond_signal(&pool->done_cond);
        }
    }
}

static MultiFileThreadPool *search_pool_create(uint32_t thread_count) {
    if (thread_count == 0) {
        thread_count = 1;
    }

    MultiFileThreadPool *pool = calloc(1, sizeof(*pool));
    if (pool == NULL) {
        fprintf(stderr, "calloc failed while creating search thread pool\n");
        return NULL;
    }
    pool->thread_count = thread_count;
    pool->workers = calloc(thread_count, sizeof(*pool->workers));
    if (pool->workers == NULL) {
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
        if (done_cond_ready) pthread_cond_destroy(&pool->done_cond);
        if (start_cond_ready) pthread_cond_destroy(&pool->start_cond);
        if (mutex_ready) pthread_mutex_destroy(&pool->mutex);
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

static void search_pool_destroy(MultiFileThreadPool *pool) {
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

static int search_pool_run_all(MultiFileThreadPool *pool, AllSearchJob *job) {
    if (pthread_mutex_init(&job->queue_mutex, NULL) != 0) {
        return 1;
    }
    job->next_file_index = 0;

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

    pthread_mutex_destroy(&job->queue_mutex);

    for (uint32_t i = 0; i < pool->thread_count; i++) {
        if (pool->workers[i].error != 0) {
            return 1;
        }
    }
    return 0;
}

static int compare_match_results(const void *lhs, const void *rhs) {
    const MatchResult *a = (const MatchResult *)lhs;
    const MatchResult *b = (const MatchResult *)rhs;
    if (a->file_id != b->file_id) {
        return a->file_id < b->file_id ? -1 : 1;
    }
    if (a->line_no != b->line_no) {
        return a->line_no < b->line_no ? -1 : 1;
    }
    return 0;
}

static int run_match_all_files(const TraceStore *store,
                               MultiFileThreadPool *pool,
                               const char *query,
                               uint64_t limit) {
    BmhSearcher searcher;
    if (bmh_init(&searcher, query) != 0) {
        return 1;
    }
    bool complete_query_bits = false;
    AsciiBits query_bits = ascii_bits_for_query(query, &complete_query_bits);

    AllSearchJob job;
    memset(&job, 0, sizeof(job));
    job.store = store;
    job.searcher = &searcher;
    job.query_bits = query_bits;
    job.use_bit_filter = complete_query_bits;
    job.from_line = 1;
    job.limit = limit;

    int result = search_pool_run_all(pool, &job);
    if (result != 0) {
        bmh_destroy(&searcher);
        return result;
    }

    uint64_t total = 0;
    for (uint32_t i = 0; i < pool->thread_count; i++) {
        total += pool->workers[i].result_count;
    }

    MatchResult *merged = NULL;
    if (total > 0) {
        merged = malloc((size_t)total * sizeof(*merged));
        if (merged == NULL) {
            bmh_destroy(&searcher);
            return 1;
        }
    }

    uint64_t pos = 0;
    for (uint32_t i = 0; i < pool->thread_count; i++) {
        SearchWorker *worker = &pool->workers[i];
        if (worker->result_count > 0) {
            memcpy(merged + pos, worker->results, (size_t)worker->result_count * sizeof(*merged));
            pos += worker->result_count;
        }
    }

    qsort(merged, total, sizeof(*merged), compare_match_results);
    result = emit_matches(store, merged, total);
    free(merged);
    bmh_destroy(&searcher);
    return result;
}

static int run_context(const IndexedFile *file,
                       uint64_t target_line,
                       uint64_t before,
                       uint64_t after) {
    if (file->mapped.size == 0) {
        return 0;
    }

    uint64_t first_line = target_line > before ? target_line - before : 1;
    uint64_t last_line = UINT64_MAX - target_line < after ? UINT64_MAX : target_line + after;
    if (last_line > file->index.count) {
        last_line = file->index.count;
    }

    size_t offset = 0;
    if (!indexed_line_start(file, first_line, &offset)) {
        return 0;
    }

    for (uint64_t line_no = first_line; line_no <= last_line; line_no++) {
        offset = line_index_offset(&file->index, line_no);
        LineView line = line_at_offset(file->mapped.data, file->mapped.size, offset);
        emit_line("context", file->id, line_no, (uint64_t)offset, line_no == target_line, line);
    }
    return 0;
}

static int handle_daemon_list(const TraceStore *store) {
    for (uint32_t i = 0; i < store->count; i++) {
        const IndexedFile *file = &store->files[i];
        double mb = (double)file->mapped.size / (1024.0 * 1024.0);
        printf("{\"type\":\"file\",\"file_id\":%" PRIu32 ",\"size_mb\":%.3f,\"line_count\":%" PRIu64
               "}\n",
               file->id,
               mb,
               file->index.count);
    }
    return emit_daemon_end("ok", NULL);
}

static int handle_daemon_match(const TraceStore *store,
                               char **parts,
                               int count) {
    if (count != 6) {
        return emit_daemon_end("error", "invalid match command");
    }

    uint32_t file_id = 0;
    if (!parse_u32(parts[1], &file_id)) {
        return emit_daemon_end("error", "invalid file id");
    }

    uint64_t from_line = 0;
    uint64_t before_line = 0;
    uint64_t limit = 0;
    if (!parse_u64(parts[2], &from_line) ||
        !parse_u64(parts[3], &before_line) ||
        !parse_u64(parts[4], &limit)) {
        return emit_daemon_end("error", "invalid numeric match argument");
    }
    if (limit == 0) {
        return emit_daemon_end("ok", NULL);
    }

    if ((from_line == 0 && before_line == 0) || (from_line != 0 && before_line != 0)) {
        return emit_daemon_end("error", "match requires exactly one of from_line or before_line");
    }

    char *query = hex_decode_to_cstr(parts[5]);
    if (query == NULL || query[0] == '\0') {
        free(query);
        return emit_daemon_end("error", "invalid or empty query");
    }

    IndexedFile *file = trace_store_get_file(store, file_id);
    if (file == NULL) {
        free(query);
        return emit_daemon_end("error", "file id not found");
    }
    int result = run_match_single_file(store, file, query, from_line, before_line, limit);

    free(query);
    if (result != 0) {
        return emit_daemon_end("error", "match failed");
    }
    return emit_daemon_end("ok", NULL);
}

static int handle_daemon_trace_all_search(const TraceStore *store,
                                          MultiFileThreadPool *pool,
                                          char **parts,
                                          int count) {
    if (count != 3) {
        return emit_daemon_end("error", "invalid trace_all_search command");
    }

    uint64_t limit = 0;
    if (!parse_u64(parts[1], &limit) || limit < 1 || limit > 10) {
        return emit_daemon_end("error", "trace_all_search limit must be between 1 and 10");
    }

    char *query = hex_decode_to_cstr(parts[2]);
    if (query == NULL || query[0] == '\0') {
        free(query);
        return emit_daemon_end("error", "invalid or empty query");
    }

    int result = run_match_all_files(store, pool, query, limit);
    free(query);
    if (result != 0) {
        return emit_daemon_end("error", "trace_all_search failed");
    }
    return emit_daemon_end("ok", NULL);
}

static int handle_daemon_context(const TraceStore *store, char **parts, int count) {
    if (count != 5) {
        return emit_daemon_end("error", "invalid context command");
    }

    uint32_t file_id = 0;
    uint64_t line = 0;
    uint64_t before = 0;
    uint64_t after = 0;
    if (!parse_u32(parts[1], &file_id) ||
        !parse_u64(parts[2], &line) ||
        !parse_u64(parts[3], &before) ||
        !parse_u64(parts[4], &after) ||
        line == 0) {
        return emit_daemon_end("error", "invalid numeric context argument");
    }

    IndexedFile *file = trace_store_get_file(store, file_id);
    if (file == NULL) {
        return emit_daemon_end("error", "file id not found");
    }

    int result = run_context(file, line, before, after);
    if (result != 0) {
        return emit_daemon_end("error", "context failed");
    }
    return emit_daemon_end("ok", NULL);
}

static int cmd_daemon(int argc, char **argv) {
    const char *dir_path = NULL;

    if (argc == 3 && strcmp(argv[2], "--help") != 0 && strcmp(argv[1], "daemon") == 0) {
        dir_path = argv[2];
    }
    for (int i = 2; i < argc; i++) {
        if (strcmp(argv[i], "--dir") == 0 && i + 1 < argc) {
            dir_path = argv[++i];
        } else if (strcmp(argv[i], "--help") == 0) {
            usage(stdout);
            return 0;
        } else if (i != 2 || dir_path == NULL || strcmp(argv[1], "daemon") != 0) {
            usage(stderr);
            return 2;
        }
    }

    if (dir_path == NULL) {
        usage(stderr);
        return 2;
    }

    TraceStore store;
    if (trace_store_open_dir(dir_path, &store) != 0) {
        return 1;
    }

    MultiFileThreadPool *pool = search_pool_create(default_search_thread_count());
    if (pool == NULL) {
        trace_store_destroy(&store);
        return 1;
    }

    printf("{\"type\":\"daemon_ready\",\"status\":\"ok\",\"file_count\":%" PRIu32
           ",\"search_threads\":%" PRIu32 "}\n",
           store.count,
           pool->thread_count);
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

        char *parts[7] = {0};
        int count = 0;
        char *saveptr = NULL;
        char *token = strtok_r(command, "\t", &saveptr);
        while (token != NULL && count < 7) {
            parts[count++] = token;
            token = strtok_r(NULL, "\t", &saveptr);
        }
        if (token != NULL) {
            emit_daemon_end("error", "too many command fields");
            continue;
        }

        if (count > 0 && strcmp(parts[0], "list") == 0) {
            handle_daemon_list(&store);
        } else if (count > 0 && strcmp(parts[0], "trace_all_search") == 0) {
            handle_daemon_trace_all_search(&store, pool, parts, count);
        } else if (count > 0 && strcmp(parts[0], "match") == 0) {
            handle_daemon_match(&store, parts, count);
        } else if (count > 0 && strcmp(parts[0], "context") == 0) {
            handle_daemon_context(&store, parts, count);
        } else {
            emit_daemon_end("error", "unknown daemon command");
        }
    }

    search_pool_destroy(pool);
    trace_store_destroy(&store);
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
    if (strcmp(argv[1], "daemon") == 0) {
        return cmd_daemon(argc, argv);
    }
    if (strcmp(argv[1], "selftest") == 0) {
        return cmd_selftest();
    }
    usage(stderr);
    return 2;
}
