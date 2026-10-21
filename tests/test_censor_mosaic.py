"""画面网格检测的用例 —— 用合成样本证明「机制正确」，不是证明「线上够准」。

真实片源的准确率必须靠人工复核标注集来评估，这一层只保证：
1. 给一块真正的块平均马赛克（块内被抹平、块边界有台阶），一定命中；
2. 给没有这种结构的普通纹理，不许命中；
3. 黑边、纯色这些"假台阶"不会造成误报。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")

from censor.mosaic_cv import (  # noqa: E402
    MosaicParams, analyze_frame, analyze_frames, numpy_available,
)
from censor.sampling import SampledFrame  # noqa: E402

W, H = 640, 360


def _textured(seed: int = 0, width: int = W, height: int = H) -> "np.ndarray":
    """造一张"看起来像自然画面"的图：低频渐变 + 中频纹理 + 噪声。"""
    rng = np.random.default_rng(seed)
    ys, xs = np.mgrid[0:height, 0:width]
    base = 70.0 + 35.0 * np.sin(xs / 41.0) + 25.0 * np.cos(ys / 27.0)
    mid = 18.0 * np.sin((xs + ys) / 9.0) * np.cos(xs / 13.0)
    noise = rng.normal(0.0, 7.0, (height, width))
    return np.clip(base + mid + noise, 0, 255).astype(np.float32)


def _apply_mosaic(img: "np.ndarray", box, block: int) -> "np.ndarray":
    """块平均马赛克 —— 与日本 AV 的打码方式一致（每块填该块均值）。"""
    out = img.copy()
    x0, y0, x1, y1 = box
    region = out[y0:y1, x0:x1]
    h, w = region.shape
    hh, ww = h // block * block, w // block * block
    means = region[:hh, :ww].reshape(hh // block, block, ww // block, block).mean(axis=(1, 3))
    region[:hh, :ww] = np.repeat(np.repeat(means, block, axis=0), block, axis=1)
    out[y0:y1, x0:x1] = region
    return out


def _frame(img: "np.ndarray", ts: float = 0.0) -> SampledFrame:
    return SampledFrame(timestamp=ts, width=img.shape[1], height=img.shape[0],
                        data=np.ascontiguousarray(img.astype(np.uint8)).tobytes(),
                        channels=1)


def test_numpy_available():
    ok, why = numpy_available()
    assert ok, why


@pytest.mark.parametrize("block", [16, 24])
def test_synthetic_mosaic_is_detected(block):
    """马赛克区要大到能被 160px 窗口完整罩住 —— 这也是真实片源的常态。"""
    img = _textured()
    patched = _apply_mosaic(img, (96, 40, W - 96, H - 40), block)
    hit = analyze_frame(_frame(patched), MosaicParams())
    assert hit.score > MosaicParams().strong_score
    assert abs(hit.block_size - block) <= 2


def test_plain_texture_is_not_detected():
    for seed in range(3):
        hit = analyze_frame(_frame(_textured(seed)), MosaicParams())
        assert hit.score < MosaicParams().strong_score


def test_letterbox_black_bars_do_not_fire():
    """``scale+pad`` 补出来的黑边不能被当成网格。"""
    img = np.zeros((H, W), np.float32)
    img[60:H - 60, :] = _textured(width=W, height=H - 120)
    hit = analyze_frame(_frame(img), MosaicParams())
    assert hit.score < MosaicParams().strong_score


def test_flat_frame_returns_empty_hit():
    hit = analyze_frame(_frame(np.full((H, W), 128, np.float32)), MosaicParams())
    assert hit.score == 0.0


def test_analyze_frames_orders_by_score_and_limits():
    plain = _textured(0)
    patched = _apply_mosaic(_textured(1), (80, 60, 560, 300), 20)
    frames = [_frame(plain, 0.0), _frame(patched, 10.0), _frame(plain, 20.0)]
    candidates, max_score = analyze_frames(frames, MosaicParams(), top=2)
    assert max_score > MosaicParams().strong_score
    assert len(candidates) <= 2
    assert candidates[0].timestamp == 10.0
    assert candidates[0].block_size > 0


def test_should_stop_breaks_early():
    frames = [_frame(_textured(i)) for i in range(4)]
    candidates, _ = analyze_frames(frames, MosaicParams(), should_stop=lambda: True)
    assert candidates == []
