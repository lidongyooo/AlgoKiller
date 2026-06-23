CC ?= cc
CFLAGS ?= -O3 -std=c11 -Wall -Wextra -Wpedantic
LDFLAGS ?=
THREAD_FLAGS ?= -pthread

TARGET := ak_search
SRC := search.c

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) $(THREAD_FLAGS) $(SRC) -o $(TARGET) $(LDFLAGS) $(THREAD_FLAGS)

clean:
	rm -f $(TARGET)
