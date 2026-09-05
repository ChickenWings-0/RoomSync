# 1D normalized spatial mapping constants [0.0 - 1.0]
# Traversal is counter-clockwise starting from the shared origin at center-right of the Back Wall

STRIP_MAPPING = {
    "OA10 33": {"pixels": 77, "t_start": 0.0000, "t_end": 0.2016},
    "OA10 30": {"pixels": 152, "t_start": 0.2016, "t_end": 0.5995},
    "OA10 20": {"pixels": 153, "t_start": 0.5995, "t_end": 1.0000},
}

STRIP_MIDPOINTS = {
    "OA10 33": 0.1008,   # midpoint of [0.0000, 0.2016] - Back Wall
    "OA10 30": 0.4006,   # midpoint of [0.2016, 0.5995] - Left Wall
    "OA10 20": 0.7998,   # midpoint of [0.5995, 1.0000] - Right Wall
}
