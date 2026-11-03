"""Check actual packaged desktop rendering, scaling, edges, and child cleanup."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile

from PIL import Image, ImageChops

from verify_stage1_package import assert_clean_log, service_exchange, wait_pid_gone, wait_report

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "build/stage2"


def verify(package: Path, report: Path) -> None:
    report.mkdir(parents=True, exist_ok=True)
    cases = [("godette", scale, "idle", edge) for scale in (0.5, 1.0, 1.5, 2.0) for edge in (-1, 1)]
    cases += [("godette", 1.0, "fall", 1)]
    results = []
    with tempfile.TemporaryDirectory(prefix="Pet3D-visual-") as temporary:
        for index, (id_, scale, action, edge) in enumerate(cases):
            data = Path(temporary) / str(index)
            service_exchange(package, data, [
                {"version": 1, "id": "save", "type": "settings.update", "payload": {
                    "character_id": id_, "scale": scale,
                    "placement": {"x": 0 if edge < 0 else 1, "y": 1, "edge_x": edge, "edge_y": 1}}},
                {"version": 1, "id": "stop", "type": "shutdown", "payload": {}},
            ])
            stem = f"{id_}-{action}-{scale}-{edge}"
            screenshot = report / (stem + ".png")
            status = report / (stem + ".json")
            log = report / (stem + ".log")
            command = [str(package / "Pet3D.exe"), "--log-file", str(log), "--",
                       f"--data-dir={data}", f"--action={action}", f"--capture={screenshot}",
                       "--capture-at=0.8", f"--report={status}", "--auto-exit=1.1"]
            process = subprocess.Popen(command, cwd=package, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                ready = wait_report(status, lambda item: item.get("service_state") == "ready")
                child = int(ready["service_pid"])
                if process.wait(timeout=20):
                    raise AssertionError(stem + " app failed")
                wait_pid_gone(child)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
            assert_clean_log(log)
            if "CAPTURE result=0" not in log.read_text("utf-8"):
                raise AssertionError(stem + " capture failed")
            image = Image.open(screenshot).convert("RGBA")
            silhouette = image.getchannel("A").point(lambda value: 255 if value > 16 else 0)
            box = silhouette.getbbox()
            if box is None or box[0] < 1 or box[1] < 1 or box[2] >= image.width or box[3] >= image.height:
                raise AssertionError(f"{stem} blank or clipped: {box}, size={image.size}")
            final = json.loads(status.read_text("utf-8"))
            if final["character_details"]["id"] != id_:
                raise AssertionError(stem + " wrong character")
            x, y, width, height = final["visible_rect"]
            wx, wy = final["window_position"]
            sx, sy, sw, sh = final["work_area"]
            if wx + x < sx - 2 or wy + y < sy - 2 or wx + x + width > sx + sw + 2 or wy + y + height > sy + sh + 2:
                raise AssertionError(stem + " outside usable desktop")
            results.append({"character": id_, "action": action, "scale": scale, "edge": edge,
                            "pixel_bounds": box, "foot_gap_px": final["foot_gap_px"], "service_cleanup": True})
            print("PASS visual " + stem, flush=True)
    # The deterministic 210-frame review must contain actual geometry below its labels.
    pose_images = sorted((WORKSPACE / "reports/poses").glob("*-front-*.png")) + sorted((WORKSPACE / "reports/poses").glob("*-side-*.png"))
    if len(pose_images) != 70:
        raise AssertionError("Expected 70 Godette pose images")
    for path in pose_images:
        image = Image.open(path).convert("RGB")
        body = image.crop((0, 50, image.width, image.height))
        background = Image.new("RGB", body.size, image.getpixel((0, image.height - 1)))
        if ImageChops.difference(body, background).getbbox() is None:
            raise AssertionError("Blank model: " + path.name)
    (report / "verification.json").write_text(json.dumps({"cases": results, "nonblank_pose_frames": len(pose_images)}, indent=2), "utf-8")
    print("PACKAGE_VISUALS_OK cases=9 pose_frames=70", flush=True)


if __name__ == "__main__":
    verify(ROOT / "dist/stage-2-godette", WORKSPACE / "reports/packaged-visuals")
