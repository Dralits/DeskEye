package com.dralit.DeskEye

import fi.iki.elonen.NanoHTTPD
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import java.io.InputStream
import java.util.concurrent.LinkedBlockingQueue


class MjpegHttpServer(
    port: Int,
    private val frameRepository: FrameRepository,
    private val onToggle: () -> Unit,
    private val onRotateRight: () -> Unit = {},
    private val onRotateLeft: () -> Unit = {}
) : NanoHTTPD(port) {

    companion object {
        private const val BOUNDARY = "frameboundary"
        private const val QUEUE_CAPACITY = 4
    }

    private val serverScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun serve(session: IHTTPSession): Response {
        return when (session.uri) {
            "/stream" -> serveStream()
            "/rotation" -> serveRotation()
            "/toggle" -> serveToggle()
            "/rturn" -> serveRotateRight()
            "/lturn" -> serveRotateLeft()
            "/", "/index.html" -> serveIndexPage()
            else -> newFixedLengthResponse(
                Response.Status.NOT_FOUND, "text/plain", "404 Not Found"
            )
        }
    }

    private fun serveToggle(): Response {
        onToggle()
        return newFixedLengthResponse(Response.Status.OK, "text/plain", "Camera toggled")
    }

    private fun serveRotation(): Response {
        val rotation = frameRepository.frames.replayCache.firstOrNull()?.rotationDegrees ?: 0
        val response = newFixedLengthResponse(Response.Status.OK, "text/plain", rotation.toString())
        response.addHeader("Cache-Control", "no-store")
        return response
    }

    private fun serveRotateRight(): Response {
        onRotateRight()
        return newFixedLengthResponse(Response.Status.OK, "text/plain", "Camera rotated right 90°")
    }

    private fun serveRotateLeft(): Response {
        onRotateLeft()
        return newFixedLengthResponse(Response.Status.OK, "text/plain", "Camera rotated left 90°")
    }

    private fun serveStream(): Response {
        // Cola de "chunks" multipart ya formateados, pendientes de enviar a ESTE cliente.
        val queue = LinkedBlockingQueue<ByteArray>(QUEUE_CAPACITY)

        val job = serverScope.launch {
            try {
                frameRepository.frames.collect { frame ->
                    queue.put(buildMultipartChunk(frame))
                }
            } catch (_: InterruptedException) {
                // Esperado al cerrar la conexión.
            } finally {
                queue.offer(ByteArray(0))
            }
        }

        val stream = QueueInputStream(queue) { job.cancel() }

        val response = newChunkedResponse(
            Response.Status.OK,
            "multipart/x-mixed-replace; boundary=$BOUNDARY",
            stream
        )
        response.addHeader("Cache-Control", "no-cache, private")
        response.addHeader("Pragma", "no-cache")
        response.addHeader("Connection", "close")
        return response
    }

    private fun buildMultipartChunk(frame: JpegFrame): ByteArray {
        val jpeg = frame.jpeg
        val header = (
            "--$BOUNDARY\r\n" +
            "Content-Type: image/jpeg\r\n" +
            "Content-Length: ${jpeg.size}\r\n" +
            "X-Rotation: ${frame.rotationDegrees}\r\n\r\n"
        ).toByteArray(Charsets.US_ASCII)
        val footer = "\r\n".toByteArray(Charsets.US_ASCII)

        val chunk = ByteArray(header.size + jpeg.size + footer.size)
        System.arraycopy(header, 0, chunk, 0, header.size)
        System.arraycopy(jpeg, 0, chunk, header.size, jpeg.size)
        System.arraycopy(footer, 0, chunk, header.size + jpeg.size, footer.size)
        return chunk
    }

    private fun serveIndexPage(): Response {
        val html = """
            <!DOCTYPE html>
            <html>
              <head>
                <title>DeskEye</title>
                <meta name="viewport" content="width=device-width, initial-scale=1" />
              </head>
              <body style="margin:0;background:#000;display:flex;align-items:center;justify-content:center;height:100vh;overflow:hidden;">
                <img id="v" src="/stream" style="max-width:100vw;max-height:100vh;" alt="stream" />
                <script>
                  var img = document.getElementById('v');
                  function syncRotation() {
                    fetch('/rotation', {cache: 'no-store'})
                      .then(function (r) { return r.text(); })
                      .then(function (t) {
                        var r = parseInt(t, 10) || 0;
                        var sideways = (r === 90 || r === 270);
                        img.style.maxWidth = sideways ? '100vh' : '100vw';
                        img.style.maxHeight = sideways ? '100vw' : '100vh';
                        img.style.transform = 'rotate(' + r + 'deg)';
                      })
                      .catch(function () {});
                  }
                  syncRotation();
                  setInterval(syncRotation, 500);
                </script>
              </body>
            </html>
        """.trimIndent()
        return newFixedLengthResponse(Response.Status.OK, "text/html", html)
    }

    override fun stop() {
        serverScope.cancel()
        super.stop()
    }

    /**
     * InputStream que lee bloques de bytes desde una [LinkedBlockingQueue].
     * Un array vacío actúa como marca de fin de stream ("poison pill").
     */
    private class QueueInputStream(
        private val queue: LinkedBlockingQueue<ByteArray>,
        private val onClose: () -> Unit
    ) : InputStream() {

        private var current: ByteArray? = null
        private var pos = 0
        @Volatile private var closed = false

        override fun read(): Int {
            val single = ByteArray(1)
            val n = read(single, 0, 1)
            return if (n <= 0) -1 else (single[0].toInt() and 0xFF)
        }

        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (closed) return -1

            while (current == null || pos >= current!!.size) {
                val next = try {
                    queue.take()
                } catch (e: InterruptedException) {
                    return -1
                }
                if (next.isEmpty()) return -1 // poison pill
                current = next
                pos = 0
            }

            val toCopy = minOf(current!!.size - pos, len)
            System.arraycopy(current!!, pos, b, off, toCopy)
            pos += toCopy
            return toCopy
        }

        override fun close() {
            if (closed) return
            closed = true
            queue.clear()
            queue.offer(ByteArray(0))
            onClose()
        }
    }
}
