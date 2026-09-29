package com.dralit.DeskEye

import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow

class JpegFrame(val jpeg: ByteArray, val rotationDegrees: Int)


class FrameRepository {

    private val _frames = MutableSharedFlow<JpegFrame>(
        replay = 1,
        extraBufferCapacity = 2,
        onBufferOverflow = BufferOverflow.DROP_OLDEST
    )

    /** Flujo de solo lectura de los frames JPEG más recientes. */
    val frames: SharedFlow<JpegFrame> = _frames.asSharedFlow()


    fun updateFrame(frame: JpegFrame) {
        _frames.tryEmit(frame)
    }
}
