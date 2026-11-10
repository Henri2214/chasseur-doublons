#!/usr/bin/env python3
"""Mesures de performance et de mémoire sur un jeu d'images SYNTHÉTIQUES (aucune photo personnelle).

  python tools/bench.py --n 15000            # génère 15 000 images, analyse (visuel), 2e analyse, doublons
  python tools/bench.py --pairs 100000       # recherche de candidats pHash sur 100 000 empreintes

Affiche : images/s, durée de la seconde analyse (doit être quasi instantanée), mémoire maximale.
"""
import argparse
import os
import shutil
import sys
import tempfile
import threading
import time

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class PeakRSS:
    """Mémoire résidente maximale (psutil si disponible, sinon /proc ou resource)."""

    def __init__(self):
        self.peak = 0
        self._stop = False
        try:
            import psutil
            self._p = psutil.Process()
        except ImportError:
            self._p = None

    def rss(self):
        if self._p:
            return self._p.memory_info().rss
        try:
            with open("/proc/self/statm") as fh:
                return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except OSError:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024

    def __enter__(self):
        def loop():
            while not self._stop:
                self.peak = max(self.peak, self.rss())
                time.sleep(0.2)
        self._t = threading.Thread(target=loop, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop = True
        self._t.join()


def gen(folder, n, size=(800, 600)):
    rng = np.random.default_rng(0)
    bases = [rng.integers(0, 255, (24, 32, 3), dtype=np.uint8) for _ in range(max(1, n // 10))]
    for i in range(n):
        b = bases[i % len(bases)].copy()
        if i % 10:                       # 9 images sur 10 : variante unique ; 1 sur 10 : copie
            b = rng.integers(0, 255, (24, 32, 3), dtype=np.uint8)
        im = Image.fromarray(b).resize(size, Image.BICUBIC)
        sub = os.path.join(folder, "d%03d" % (i // 1000))
        os.makedirs(sub, exist_ok=True)
        im.save(os.path.join(sub, "img_%06d.jpg" % i), quality=85)


def bench_scan(n, keep):
    from core.web import State
    from core import dupes
    tmp = tempfile.mkdtemp(prefix="cd_bench_")
    try:
        root = os.path.join(tmp, "photos")
        t = time.time()
        gen(root, n)
        print("génération de %d images : %.1f s" % (n, time.time() - t))
        st = State(data_dir=os.path.join(tmp, "data"), models_dir=os.path.join(tmp, "models"))
        with PeakRSS() as m:
            t = time.time()
            s = st.scanner.scan_sync(root, {"videos": False})
            t1 = time.time() - t
            t = time.time()
            st.scanner.scan_sync(root, {"videos": False})
            t2 = time.time() - t
            t = time.time()
            g = dupes.compute(st.db, [root], {"rigor": "strict"})
            t3 = time.time() - t
        print("1re analyse : %.1f s (%.1f images/s)" % (t1, n / t1))
        for k, v in (s.get("per_stage") or {}).items():
            print("   %-12s %7.1f ms/image  ~%s images/s" % (k, v["ms_par_image"], v["images_par_s"]))
        print("2e analyse sans changement : %.2f s" % t2)
        print("calcul des doublons : %.2f s, %d groupes" % (t3, len(g)))
        print("mémoire maximale : %.0f Mo" % (m.peak / 2**20))
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def bench_pairs(n):
    from core.dupes import candidate_pairs
    rng = np.random.default_rng(1)
    H = rng.integers(0, 2**63, n, dtype=np.uint64)
    H[::50] = H[1::50][:len(H[::50])] ^ np.uint64(3)   # quelques quasi-doublons
    with PeakRSS() as m:
        t = time.time()
        ci, cj, _ = candidate_pairs(H, 7)
        dt = time.time() - t
    print("%d empreintes : %d paires candidates en %.1f s, mémoire max %.0f Mo" % (n, len(ci), dt, m.peak / 2**20))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=0)
    ap.add_argument("--pairs", type=int, default=0)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    if a.pairs:
        bench_pairs(a.pairs)
    if a.n:
        bench_scan(a.n, a.keep)
    if not a.n and not a.pairs:
        ap.print_help()
