extends RefCounted

static func clamp_window(position: Vector2i, size: Vector2i, work_area: Rect2i) -> Vector2i:
	var maximum := work_area.position + work_area.size - size
	return Vector2i(
		clampi(position.x, work_area.position.x, maxi(work_area.position.x, maximum.x)),
		clampi(position.y, work_area.position.y, maxi(work_area.position.y, maximum.y))
	)

static func clamp_subject(position: Vector2i, subject: Rect2, work_area: Rect2i) -> Vector2i:
	# The transparent HWND may extend outside the work area. Only the visible
	# character, including its soles, must stay within the desktop boundaries.
	var minimum := Vector2i(ceil(Vector2(work_area.position).x - subject.position.x), ceil(Vector2(work_area.position).y - subject.position.y))
	var maximum := Vector2i(floor(Vector2(work_area.end).x - subject.end.x), floor(Vector2(work_area.end).y - subject.end.y))
	return Vector2i(clampi(position.x, minimum.x, maxi(minimum.x, maximum.x)), clampi(position.y, minimum.y, maxi(minimum.y, maximum.y)))

static func pin_edges(position: Vector2i, subject: Rect2, work_area: Rect2i, edges: Vector2i) -> Vector2i:
	var requested := position
	if edges.x != 0:
		requested.x = ceili(work_area.position.x - subject.position.x) if edges.x < 0 else floori(work_area.end.x - subject.end.x)
	if edges.y != 0:
		requested.y = ceili(work_area.position.y - subject.position.y) if edges.y < 0 else floori(work_area.end.y - subject.end.y)
	return clamp_subject(requested, subject, work_area)

static func edge_flags(position: Vector2i, subject: Rect2, work_area: Rect2i) -> Vector2i:
	var start := Vector2(position) + subject.position
	var end := Vector2(position) + subject.end
	return Vector2i(
		-1 if absf(start.x - work_area.position.x) < 1.1 else (1 if absf(end.x - work_area.end.x) < 1.1 else 0),
		-1 if absf(start.y - work_area.position.y) < 1.1 else (1 if absf(end.y - work_area.end.y) < 1.1 else 0)
	)

static func pointer_in_subject(cursor: Vector2i, position: Vector2i, subject: Rect2, work_area: Rect2i) -> bool:
	return work_area.has_point(cursor) and subject.has_point(Vector2(cursor - position))
