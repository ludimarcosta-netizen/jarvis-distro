"""Comparação de vozes de reuniões; nunca concede permissões ou altera biometria."""
import numpy as np

MIN_SCORE = 0.72
MIN_MARGIN = 0.12
MIN_WINDOWS = 3
MIN_CLEAN_SECONDS = 9.0
VERSION = 3


def valid_vectors(vectors: list, dimension: int | None = None) -> list[np.ndarray]:
    result = []
    for value in vectors:
        vector = np.asarray(value, dtype=np.float64)
        if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
            continue
        if dimension is not None and len(vector) != dimension:
            continue
        size = float(np.linalg.norm(vector))
        if size > 1e-9:
            result.append(vector / size)
    return result


def _project(vectors: list[np.ndarray], center: np.ndarray) -> list[np.ndarray]:
    return valid_vectors([(v - center).tolist() for v in vectors])


def calibrate(profiles: dict[str, list]) -> dict:
    """Validação interna: cada amostra compara com as demais, nunca consigo mesma."""
    groups = {name: valid_vectors(vectors) for name, vectors in profiles.items()}
    groups = {name: vectors for name, vectors in groups.items() if vectors}
    if not groups:
        return {"ready": False, "separable": False, "threshold": MIN_SCORE, "margin": MIN_MARGIN,
                "checked_windows": 0, "center": [], "profiles": {}}
    dimension = len(next(iter(groups.values()))[0])
    groups = {n: [v for v in vs if len(v) == dimension] for n, vs in groups.items()}
    groups = {n: vs for n, vs in groups.items() if vs}
    # Centro aprendido só com referências. A presença de outra voz na reunião
    # nunca altera o espaço de comparação, como acontecia no fluxo anterior.
    center = (np.mean([np.mean(vs, axis=0) for vs in groups.values()], axis=0)
              if len(groups) >= 3 else np.zeros(dimension))
    groups = {n: _project(vs, center) for n, vs in groups.items()}
    results = {}
    for name, vectors in groups.items():
        positives, negatives = [], []
        ready = len(groups) >= 2 and len(vectors) >= MIN_WINDOWS
        if ready:
            for index, vector in enumerate(vectors):
                others = [v for i, v in enumerate(vectors) if i != index and len(v) == len(vector)]
                rivals = [v for n, vs in groups.items() if n != name for v in vs if len(v) == len(vector)]
                if not others or not rivals:
                    ready = False
                    continue
                positives.append(max(float(vector @ v) for v in others))
                negatives.append(max(float(vector @ v) for v in rivals))
        # Uma referência conflitante não invalida as demais. Exige pelo menos
        # três referências com suporte mútuo, sempre contra TODOS os rivais.
        indices = list(range(len(positives)))
        while len(indices) >= MIN_WINDOWS:
            supported = [i for i in indices
                         if max(float(vectors[i] @ vectors[j]) for j in indices if j != i)
                         >= max(MIN_SCORE, negatives[i] + MIN_MARGIN)]
            if supported == indices:
                break
            indices = supported
        if len(indices) < MIN_WINDOWS:
            indices = []
        threshold = max(MIN_SCORE, max((negatives[i] for i in indices), default=0) + 0.02)
        separable = (ready and bool(indices)
                     and all(max(float(vectors[i] @ vectors[j]) for j in indices if j != i) >= threshold
                             for i in indices))
        results[name] = {"ready": ready, "separable": separable, "threshold": threshold,
                         "margin": MIN_MARGIN, "checked_windows": len(positives),
                         "reference_indices": indices}
    return {"ready": any(r["ready"] for r in results.values()),
            "separable": any(r["separable"] for r in results.values()),
            "threshold": max((r["threshold"] for r in results.values()), default=MIN_SCORE),
            "margin": MIN_MARGIN, "checked_windows": sum(r["checked_windows"] for r in results.values()),
            "center": center.tolist(), "profiles": results}


def match_voice(evidence: dict | list, profiles: dict[str, list], calibration: dict | None = None) -> dict | None:
    """Sugere por consenso; nomeação exige várias janelas e cadastro separável."""
    legacy = isinstance(evidence, list)
    raw = [evidence] if legacy else evidence.get("embeddings", [])
    queries = valid_vectors(raw)
    if not queries:
        return None
    dimension = len(queries[0])
    queries = [v for v in queries if len(v) == dimension]
    groups = {name: valid_vectors(vectors, dimension) for name, vectors in profiles.items()}
    groups = {name: vectors for name, vectors in groups.items() if vectors}
    if not groups:
        return None
    raw_scores = {name: [max(float(q @ v) for v in vectors) for q in queries] for name, vectors in groups.items()}
    calibration = calibration or calibrate(groups)
    center = np.asarray(calibration["center"])
    queries = _project(queries, center)
    if not queries:
        return None
    groups = {name: _project(vs, center) for name, vs in groups.items()}
    groups = {name: vs for name, vs in groups.items() if vs}
    if not groups:
        return None
    scores = {name: [max(float(q @ v) for v in vectors) for q in queries] for name, vectors in groups.items()}
    ranked = sorted(((float(np.median(values)), name) for name, values in scores.items()), reverse=True)
    score, name = ranked[0]
    margins = [value - max((values[i] for n, values in scores.items() if n != name), default=0.0)
               for i, value in enumerate(scores[name])]
    winners = [max(scores, key=lambda n, i=i: scores[n][i]) for i in range(len(queries))]
    agreement = winners.count(name) / len(winners)
    if (score < MIN_SCORE or float(np.median(margins)) < 0.05 or agreement < 2 / 3
            or float(np.median(raw_scores[name])) < MIN_SCORE):
        return None
    measured = calibration["profiles"][name]
    clean_seconds = 0 if legacy else float(evidence.get("clean_seconds", 0))
    enough = len(queries) >= MIN_WINDOWS and clean_seconds >= MIN_CLEAN_SECONDS
    automatic = (enough and measured["separable"] and agreement == 1
                 and min(scores[name]) >= measured["threshold"]
                 and min(margins) >= measured["margin"] and min(raw_scores[name]) >= MIN_SCORE)
    if automatic:
        # Sugestões ainda consideram todas as referências. Nomeação automática
        # precisa de suporte no subconjunto que passou na calibração.
        trusted = [groups[name][i] for i in measured["reference_indices"]]
        supported = [max(float(q @ v) for v in trusted) for q in queries]
        rivals = [max((values[i] for n, values in scores.items() if n != name), default=0.0)
                  for i in range(len(queries))]
        automatic = all(score >= measured["threshold"] and score - rival >= measured["margin"]
                        for score, rival in zip(supported, rivals, strict=True))
    reason = ("consenso entre trechos limpos" if automatic else
              "fala limpa insuficiente" if not enough else
              "cadastros precisam de calibração ou novas amostras" if not measured["separable"] else
              "trechos discordam ou vozes parecidas")
    return {"name": name, "score": round(score, 3), "margin": round(min(margins), 3),
            "windows": len(queries), "automatic": bool(automatic), "reason": reason,
            "calibrated": bool(measured["ready"])}
