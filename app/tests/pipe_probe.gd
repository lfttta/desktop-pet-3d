extends SceneTree
var pipe: Dictionary
var ticks := 0
func _initialize() -> void:
	pipe = OS.execute_with_pipe("D:/phthon lang/python.exe", PackedStringArray(["-u", "-c", "import sys;print('READY中文',flush=True);line=sys.stdin.buffer.readline();sys.stdout.buffer.write(line);sys.stdout.buffer.flush()"]), false)
	print("PIPE keys=", pipe.keys())
	if pipe.is_empty():
		quit(1)
		return
	print("stdio class=", pipe.stdio.get_class())
	pipe.stdio.store_buffer("hello中文\n".to_utf8_buffer())
	pipe.stdio.flush()
func _process(_delta: float) -> bool:
	ticks += 1
	var length: int = pipe.stdio.get_length()
	if length > 0:
		print("PIPE length=", length, " data=", pipe.stdio.get_buffer(length).get_string_from_utf8())
	if not OS.is_process_running(int(pipe.pid)) or ticks > 20000:
		pipe.stdio.close()
		pipe.stderr.close()
		print("PIPE done pid=", pipe.pid)
		quit(0)
	return false
