extends RefCounted

static func capsule_contains(point: Vector2, start: Vector2, end: Vector2, radius: float) -> bool:
	var segment := end - start
	var length_squared := segment.length_squared()
	var fraction := 0.0 if length_squared < 0.0001 else clampf((point - start).dot(segment) / length_squared, 0.0, 1.0)
	return point.distance_squared_to(start + fraction * segment) <= radius * radius

static func contains(point: Vector2, regions: Array) -> bool:
	for region: Dictionary in regions:
		if capsule_contains(point, region.a, region.b, region.radius):
			return true
	return false
