package com.dralit.DeskEye

import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow

/**
 * Repositorio que desacopla el productor de frames (el analyzer de CameraX,
 * que se ejecuta en su propio hilo) del consumidor (el servidor HTTP,
 * potencialmente con varios clientes a la vez).
 *
 * Usa un [MutableSharedFlow] con:
 *  - replay = 1 -> cualquier cliente que se conecte recibe inmediatamente el último frame.
 *  - DROP_OLDEST -> si un cliente va lento, se descartan frames antiguos en vez de
 *    acumular memoria o bloquear la captura de cámara.
 */
class FrameRepository {

    private val _frames = MutableSharedFlow<ByteArray>(
        replay = 1,
        extraBufferCapacity = 2,
        onBufferOverflow = BufferOverflow.DROP_OLDEST
    )

    /** Flujo de solo lectura de los frames JPEG más recientes. */
    val frames: SharedFlow<ByteArray> = _frames.asSharedFlow()

    /**
     * Publica un nuevo frame JPEG. Llamado desde el hilo del analyzer de CameraX.
     * No suspende ni bloquea: si el buffer está lleno, descarta el más antiguo.
     */
    fun updateFrame(jpegBytes: ByteArray) {
        _frames.tryEmit(jpegBytes)
    }
}
