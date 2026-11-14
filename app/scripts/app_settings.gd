extends RefCounted
## Serializable placement uses the visible sole point, independent of window padding.

static func defaults() -> Dictionary:
	return {"schema_version": 2, "character_id": "godette", "persona_id": "godette", "scale": 1.0, "screen_id": "", "user_name": "你", "placement": {"x": 1.0, "y": 1.0, "edge_x": 1, "edge_y": 1}}

static func placement(position: Vector2i, subject: Rect2, work: Rect2i, edges: Vector2i) -> Dictionary:
	var foot := Vector2(position) + Vector2(subject.get_center().x, subject.end.y)
	return {"x": clampf((foot.x - work.position.x) / maxf(1, work.size.x), 0, 1), "y": clampf((foot.y - work.position.y) / maxf(1, work.size.y), 0, 1), "edge_x": edges.x, "edge_y": edges.y}

static func position_from(saved: Dictionary, subject: Rect2, work: Rect2i) -> Vector2i:
	var foot := Vector2(work.position) + Vector2(float(saved.get("x", 1)) * work.size.x, float(saved.get("y", 1)) * work.size.y)
	return Vector2i((foot - Vector2(subject.get_center().x, subject.end.y)).round())

## Shrinks one utility window so it fits inside the work area with `margin` on
## each side. The desktop pet lives on the primary screen only, so every caller
## passes that screen's usable rect.
static func fit_window_size(window_size: Vector2i, work: Rect2i, margin := 0) -> Vector2i:
	var available := Vector2i(
		maxi(1, work.size.x - margin * 2),
		maxi(1, work.size.y - margin * 2))
	return Vector2i(mini(window_size.x, available.x), mini(window_size.y, available.y))

static func utility_position(window_size: Vector2i, work: Rect2i) -> Vector2i:
	var visible_size := Vector2i(mini(window_size.x, work.size.x), mini(window_size.y, work.size.y))
	return work.position + Vector2i((work.size.x - visible_size.x) / 2, (work.size.y - visible_size.y) / 2)
