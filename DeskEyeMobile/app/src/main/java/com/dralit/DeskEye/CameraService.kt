package com.dralit.DeskEye

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.PowerManager
import android.util.Log
import android.util.Size
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleService
import fi.iki.elonen.NanoHTTPD
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.io.IOException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

class CameraService : LifecycleService() {

    companion object {
        private const val TAG = "CameraService"
        private const val CHANNEL_ID = "CameraServiceChannel"
        private const val NOTIFICATION_ID = 1
        
        private val _isRunning = MutableStateFlow(false)
        val isRunning: StateFlow<Boolean> = _isRunning

        private val _framesServed = MutableStateFlow(0L)
        val framesServed: StateFlow<Long> = _framesServed

        private val _port = MutableStateFlow(4444)
        val port: StateFlow<Int> = _port

        private val _isBackCamera = MutableStateFlow(true)
        val isBackCamera: StateFlow<Boolean> = _isBackCamera

        const val ACTION_TOGGLE_CAMERA = "com.dralit.DeskEye.TOGGLE_CAMERA"

        private const val TARGET_FPS = 15
        private const val JPEG_QUALITY = 70
    }

    private lateinit var cameraExecutor: ExecutorService
    private var mjpegServer: MjpegHttpServer? = null
    private val frameRepository = FrameRepository()
    private var wakeLock: PowerManager.WakeLock? = null

    private var lastAnalyzedTimestampMs = 0L
    private val minFrameIntervalMs = 1000L / TARGET_FPS

    override fun onCreate() {
        super.onCreate()
        cameraExecutor = Executors.newSingleThreadExecutor()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        
        if (intent?.action == ACTION_TOGGLE_CAMERA) {
            _isBackCamera.value = !_isBackCamera.value
            if (_isRunning.value) {
                bindCamera()
            }
        } else {
            val port = intent?.getIntExtra("port", 4444) ?: 4444
            _port.value = port
            
            startForegroundService(port)
            startCameraAndServer(port)
        }
        
        return START_STICKY
    }

    private fun startForegroundService(port: Int) {
        createNotificationChannel()
        val notification: Notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("DeskEye is broadcasting")
            .setContentText("Camera stream active on port $port")
            .setSmallIcon(R.mipmap.deskeyelogo)
            .setOngoing(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID, 
                notification, 
                ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = powerManager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "DeskEye::CameraWakeLock").apply {
            acquire(10 * 60 * 1000L)
        }
    }

    private fun startCameraAndServer(port: Int) {
        try {
            if (mjpegServer == null) {
                mjpegServer = MjpegHttpServer(port, frameRepository) {
                    _isBackCamera.value = !_isBackCamera.value
                    bindCamera()
                }
                mjpegServer?.start(NanoHTTPD.SOCKET_READ_TIMEOUT, false)
            }
            _isRunning.value = true
            _framesServed.value = 0
            
            bindCamera()

        } catch (e: IOException) {
            Log.e(TAG, "Server failed to start", e)
            stopSelf()
        }
    }

    private fun bindCamera() {
        val cameraProviderFuture = ProcessCameraProvider.getInstance(this)
        cameraProviderFuture.addListener({
            val cameraProvider = cameraProviderFuture.get()
            
            val imageAnalysis = ImageAnalysis.Builder()
                .setTargetResolution(Size(640, 480))
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build()
                .also {
                    it.setAnalyzer(cameraExecutor) { imageProxy ->
                        processFrame(imageProxy)
                    }
                }

            val cameraSelector = if (_isBackCamera.value) {
                CameraSelector.DEFAULT_BACK_CAMERA
            } else {
                CameraSelector.DEFAULT_FRONT_CAMERA
            }

            try {
                cameraProvider.unbindAll()
                cameraProvider.bindToLifecycle(this, cameraSelector, imageAnalysis)
            } catch (exc: Exception) {
                Log.e(TAG, "Use case binding failed", exc)
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun processFrame(imageProxy: ImageProxy) {
        val now = System.currentTimeMillis()
        if (now - lastAnalyzedTimestampMs < minFrameIntervalMs) {
            imageProxy.close()
            return
        }
        lastAnalyzedTimestampMs = now

        try {
            val jpeg = ImageUtils.imageProxyToJpeg(imageProxy, quality = JPEG_QUALITY)
            frameRepository.updateFrame(jpeg)
            _framesServed.value++
        } catch (e: Exception) {
            Log.e(TAG, "Error processing frame", e)
        } finally {
            imageProxy.close()
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val serviceChannel = NotificationChannel(
                CHANNEL_ID,
                "DeskEye Camera Service Channel",
                NotificationManager.IMPORTANCE_LOW
            )
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(serviceChannel)
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        mjpegServer?.stop()
        mjpegServer = null
        _isRunning.value = false
        cameraExecutor.shutdown()
        wakeLock?.let {
            if (it.isHeld) it.release()
        }
        Log.d(TAG, "Service destroyed")
    }
}
