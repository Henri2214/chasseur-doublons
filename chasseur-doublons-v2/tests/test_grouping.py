"""Recherche de candidats vectorisée, regroupement sans chaînage, regroupement des visages."""
from collections import defaultdict

import numpy as np

from core.dupes import candidate_pairs, faces_ok, meta_check, reference_groups
from core.faces import cluster
from core.config import RIGOR
from core.util import popcount64


def test_candidate_pairs_identique_a_la_force_brute():
    rng = np.random.default_rng(0)
    base = rng.integers(0, 2**63, 50, dtype=np.uint64)
    H = np.concatenate([base, base ^ np.uint64(0b1011), rng.integers(0, 2**63, 300, dtype=np.uint64)])
    for blk in (7, 64, None):
        ci, cj, cd = candidate_pairs(H, 6, blk)
        got = set(zip(ci.tolist(), cj.tolist()))
        exp = {(i, j) for i in range(len(H)) for j in range(i + 1, len(H))
               if int(popcount64(np.array([H[i] ^ H[j]]))[0]) <= 6}
        assert got == exp
        assert all(i < j for i, j in got)


def test_candidate_pairs_index_multiple():
    """Chemin rapide (seuil <= 7, > 2000 empreintes) identique à la force brute."""
    from core.dupes import _candidate_pairs_brute
    rng = np.random.default_rng(4)
    H = rng.integers(0, 2**63, 6000, dtype=np.uint64)
    for k in range(0, 6000, 9):
        H[k + 1] = H[k] ^ np.uint64(int(rng.integers(0, 2**7)))
    for thr in (3, 7):
        a = candidate_pairs(H, thr)
        b = _candidate_pairs_brute(H, thr)
        assert set(zip(a[0].tolist(), a[1].tolist())) == set(zip(b[0].tolist(), b[1].tolist()))
        assert len(a[0]) > 100


def test_pas_de_chainage():
    # chaîne A~B~C~D : A et D ne se ressemblent pas, ils ne doivent jamais être dans le même groupe
    adj = defaultdict(dict)
    for a, b, s in ((0, 1, 98), (1, 2, 97), (2, 3, 96)):
        adj[a][b] = adj[b][a] = s
    groups = reference_groups(adj, lambda k: 0)
    assert groups
    for ref, mem in groups:
        members = {ref} | {m for m, _ in mem}
        assert not {0, 3} <= members
        for m in members - {ref}:
            assert m in adj[ref]          # chaque membre est relié directement à la référence


def test_reference_et_scores():
    adj = defaultdict(dict)
    for a, b, s in ((0, 1, 99), (0, 2, 95), (1, 2, 96), (3, 4, 90)):
        adj[a][b] = adj[b][a] = s
    groups = reference_groups(adj, lambda k: k)
    sets = [{r} | {m for m, _ in mem} for r, mem in groups]
    assert {0, 1, 2} in sets and {3, 4} in sets


def test_meta_date_absente_signalee():
    P = RIGOR["strict"]
    a = {"taken": 100.0, "cam": "x", "w": 4000, "h": 3000}
    b = {"taken": None, "cam": "x", "w": 2000, "h": 1500}
    ok, dated = meta_check(a, b, P)
    assert ok and not dated
    c = dict(b, taken=200.0)
    assert meta_check(a, c, P) == (False, True)
    d = dict(b, taken=100.2)
    assert meta_check(a, d, P) == (True, True)
    e = dict(b, taken=100.0, cam="autre")
    assert not meta_check(a, e, P)[0]
    f = dict(b, taken=100.0, w=3000, h=3000)      # proportions différentes
    assert not meta_check(a, f, P)[0]


def _emb(rng, center, noise=0.15):
    e = center + noise * rng.standard_normal(center.shape)
    return e / np.linalg.norm(e)


def test_faces_ok():
    rng = np.random.default_rng(1)
    p1, p2 = rng.standard_normal(512), rng.standard_normal(512)
    p1 /= np.linalg.norm(p1)
    p2 /= np.linalg.norm(p2)
    P = RIGOR["strict"]
    fa = [((0.1, 0.1, 0.3, 0.4), _emb(rng, p1, 0.02)), ((0.6, 0.1, 0.8, 0.4), _emb(rng, p2, 0.02))]
    fb = [((0.61, 0.11, 0.8, 0.41), _emb(rng, p2, 0.02)), ((0.1, 0.1, 0.31, 0.4), _emb(rng, p1, 0.02))]
    assert faces_ok(fa, fb, P)
    assert not faces_ok(fa, fb[:1], P)                                    # nombre différent
    moved = [((0.3, 0.1, 0.5, 0.4), fb[1][1]), fb[0]]
    assert not faces_ok(fa, moved, P)                                     # visage déplacé
    swapped = [(fb[0][0], fb[1][1]), (fb[1][0], fb[0][1])]
    assert not faces_ok(fa, swapped, P)                                   # personnes différentes au même endroit
    assert faces_ok(fa, [], P)                                            # une seule photo avec visages : pas de contrainte


def test_cluster_personnes_et_verrouillage():
    rng = np.random.default_rng(2)
    D = 128
    centers = [c / np.linalg.norm(c) for c in rng.standard_normal((3, D))]
    embs, truth = [], []
    for k, c in enumerate(centers):
        for _ in range(20):
            embs.append(_emb(rng, c, 0.06))
            truth.append(k)
    embs = np.stack(embs).astype(np.float32)
    truth = np.array(truth)
    locked = np.zeros(len(embs), bool)
    lpid = np.full(len(embs), -1)
    labels = cluster(embs, locked, lpid, {}, thr=0.45)
    # chaque vraie personne -> une seule étiquette, et étiquettes distinctes
    lab_of = [set(labels[truth == k].tolist()) for k in range(3)]
    assert all(len(s) == 1 for s in lab_of)
    assert len({next(iter(s)) for s in lab_of}) == 3
    # personne nommée existante (id 7) + visage verrouillé hors personne : ils ne bougent pas
    locked[0] = True
    lpid[0] = -1             # « n'appartient à personne »
    locked[25] = True
    lpid[25] = 7
    labels2 = cluster(embs, locked, lpid, {7: centers[1]}, thr=0.45)
    assert labels2[0] == -1
    assert set(labels2[truth == 1].tolist()) == {7}
