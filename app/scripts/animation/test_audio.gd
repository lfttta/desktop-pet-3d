extends RefCounted
## Original procedural test signal; no downloaded or recorded audio assets.

static func create() -> Dictionary:
	const RATE := 22050
	const DURATION := 3.0
	var pcm := PackedByteArray()
	pcm.resize(int(RATE * DURATION) * 2)
	var envelope := PackedFloat32Array()
	for index in int(RATE * DURATION):
		var time := index / float(RATE)
		var phase := fmod(time, 0.6) / 0.6
		var amplitude := sin(PI * phase / 0.72) if phase < 0.72 else 0.0
		var tone := sin(TAU * 180.0 * time) * 0.7 + sin(TAU * 360.0 * time) * 0.3
		pcm.encode_s16(index * 2, int(tone * amplitude * 8000))
		if index % 220 == 0:
			envelope.append(amplitude)
	envelope.append(0.0)
	var stream := AudioStreamWAV.new()
	stream.format = AudioStreamWAV.FORMAT_16_BITS
	stream.mix_rate = RATE
	stream.data = pcm
	return {"stream": stream, "envelope": envelope, "duration": DURATION}
