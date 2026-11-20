CXX = g++
CXXFLAGS = -O3 -std=c++17 -shared -fPIC
TARGET = dnd_engine.so
SRC = dnd_engine.cpp
PYTHON = python3
.PHONY: all build install run clean
all: build
build: $(TARGET)
(TARGET):(SRC)
(CXX)(CXXFLAGS) (SRC) -o(TARGET)
install:
$(PYTHON) -m pip install -r requirements.txt
run: build
$(PYTHON) api.py
clean:
rm -f $(TARGET) dnd_engine.dll
