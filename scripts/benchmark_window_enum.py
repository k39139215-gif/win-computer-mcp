import time

def benchmark_window_enum():
    start = time.perf_counter()
    # Simulated window enumeration lookup
    windows = [f"Window_{i}" for i in range(150)]
    found = [w for w in windows if "Window_42" in w]
    elapsed = (time.perf_counter() - start) * 1000
    print(f"Enumerated {len(windows)} windows in {elapsed:.3f} ms. Found: {found}")

if __name__ == '__main__':
    benchmark_window_enum()
