package com.dralit.DeskEye

import fi.iki.elonen.NanoHTTPD
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import java.io.InputStream
import java.util.concurrent.LinkedBlockingQueue

/**
 * Servidor HTTP embebido que expone el último frame de la cámara como un
 * stream MJPEG (`multipart/x-mixed-replace`).
 *
 * Endpoints:
 *  - GET /stream      -> stream MJPEG continuo
 *  - GET / o /index   -> página HTML mínima con un <img> apuntando a /stream
 *
 * Implementación: por cada conexión a /stream se crea una cola
 * ([LinkedBlockingQueue]) y un InputStream personalizado que NanoHTTPD va
 * leyendo. Una corrutina suscrita a [FrameRepository.frames] vuelca cada
 * frame nuevo en la cola como un "chunk" multipart ya formateado.
 *
 * Se evita deliberadamente PipedInputStream/PipedOutputStream: esas clases
 * comprueban la vivacidad de los hilos productor/consumidor, lo cual es
 * frágil quando el productor corre sobre corrutinas con dispatchers elásticos
 * como Dispatchers.IO. Una cola bloqueante no tiene ese problema y además
 * aporta backpressure natural (si un cliente va lento, `put()` bloquea el
 * productor de ESE cliente sin afectar a los demás).
 */
class MjpegHttpServer(
    port: Int,
    private val frameRepository: FrameRepository
) : NanoHTTPD(port) {

    companion object {
        private const val BOUNDARY = "frameboundary"
        private const val QUEUE_CAPACITY = 4
    }

    private val serverScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun serve(session: IHTTPSession): Response {
        return when (session.uri) {
            "/stream" -> serveStream()
            "/", "/index.html" -> serveIndexPage()
            else -> newFixedLengthResponse(
                Response.Status.NOT_FOUND, "text/plain", "404 Not Found"
            )
        }
    }

    private fun serveStream(): Response {
        // Cola de "chunks" multipart ya formateados, pendientes de enviar a ESTE cliente.
        val queue = LinkedBlockingQueue<ByteArray>(QUEUE_CAPACITY)

        val job = serverScope.launch {
            try {
                frameRepository.frames.collect { jpeg ->
                    queue.put(buildMultipartChunk(jpeg))
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

    private fun buildMultipartChunk(jpeg: ByteArray): ByteArray {
        val header = "--$BOUNDARY\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpeg.size}\r\n\r\n"
            .toByteArray(Charsets.US_ASCII)
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
              <body style="margin:0;background:#000;display:flex;align-items:center;justify-content:center;height:100vh;">
                <img src="/stream" style="max-width:100%;max-height:100%;" alt="stream" />
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
