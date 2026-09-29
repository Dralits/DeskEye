package com.dralit.DeskEye

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CaptureRequest
import android.net.wifi.WifiManager
import android.os.Build
import android.os.PowerManager
import android.util.Log
import android.util.Size
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
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

        private val _previewFrame = MutableStateFlow<JpegFrame?>(null)
        val previewFrame: StateFlow<JpegFrame?> = _previewFrame

        const val ACTION_TOGGLE_CAMERA = "com.dralit.DeskEye.TOGGLE_CAMERA"
        const val ACTION_ROTATE_RIGHT  = "com.dralit.DeskEye.ROTATE_RIGHT"
        const val ACTION_ROTATE_LEFT   = "com.dralit.DeskEye.ROTATE_LEFT"

        private const val TARGET_FPS = 30
        private const val JPEG_QUALITY = 70

        private const val MIN_SMOOTH_FPS = 24
    }

    private lateinit var cameraExecutor: ExecutorService
    private var mjpegServer: MjpegHttpServer? = null
    private val frameRepository = FrameRepository()
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    @Volatile
    private var manualRotationOffset = 0

    private var lastFrameTimestampNs = 0L
    private val minFrameIntervalNs = 1_000_000_000L / TARGET_FPS * 9 / 10

    override fun onCreate() {
        super.onCreate()
        cameraExecutor = Executors.newSingleThreadExecutor()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        
        when (intent?.action) {
            ACTION_TOGGLE_CAMERA -> {
                _isBackCamera.value = !_isBackCamera.value
                if (_isRunning.value) {
                    bindCamera()
                }
            }
            ACTION_ROTATE_RIGHT -> {
                rotateRight()
            }
            ACTION_ROTATE_LEFT -> {
                rotateLeft()
            }
            else -> {
                val port = intent?.getIntExtra("port", 4444) ?: 4444
                _port.value = port
                
                startForegroundService(port)
                startCameraAndServer(port)
            }
        }
        
        return START_STICKY
    }

    fun rotateRight() {
        manualRotationOffset = (manualRotationOffset + 90).mod(360)
        Log.d(TAG, "Rotated right: current offset=$manualRotationOffset°")
    }

    fun rotateLeft() {
        manualRotationOffset = (manualRotationOffset - 90).mod(360)
        Log.d(TAG, "Rotated left: current offset=$manualRotationOffset°")
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
        
        acquireLocks()
    }


    private fun acquireLocks() {
        if (wakeLock?.isHeld != true) {
            val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = powerManager.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK, "DeskEye::CameraWakeLock"
            ).apply {
                setReferenceCounted(false)
                acquire()
            }
        }

        if (wifiLock?.isHeld != true) {
            val wifiManager = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            // FULL_LOW_LATENCY existe desde API 29; en versiones anteriores se usa FULL_HIGH_PERF
            // (deprecado en API 34, donde ya no se usa).
            @Suppress("DEPRECATION")
            val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                WifiManager.WIFI_MODE_FULL_LOW_LATENCY
            } else {
                WifiManager.WIFI_MODE_FULL_HIGH_PERF
            }
            wifiLock = wifiManager.createWifiLock(mode, "DeskEye::WifiLock").apply {
                setReferenceCounted(false)
                acquire()
            }
        }
    }

    private fun releaseLocks() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        wifiLock?.let { if (it.isHeld) it.release() }
        wifiLock = null
    }

    private fun startCameraAndServer(port: Int) {
        try {
            if (mjpegServer == null) {
                mjpegServer = MjpegHttpServer(
                    port = port,
                    frameRepository = frameRepository,
                    onToggle = {
                        _isBackCamera.value = !_isBackCamera.value
                        bindCamera()
                    },
                    onRotateRight = {
                        rotateRight()
                    },
                    onRotateLeft = {
                        rotateLeft()
                    }
                )
                mjpegServer?.start(NanoHTTPD.SOCKET_READ_TIMEOUT, false)
            }
            _isRunning.value = true
            _framesServed.value = 0
            _previewFrame.value = null
            
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
            
            val cameraSelector = if (_isBackCamera.value) {
                CameraSelector.DEFAULT_BACK_CAMERA
            } else {
                CameraSelector.DEFAULT_FRONT_CAMERA
            }

            val analysisBuilder = ImageAnalysis.Builder()
                .setTargetResolution(Size(640, 480))
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            applySmoothFpsRange(analysisBuilder, cameraProvider, cameraSelector)

            val imageAnalysis = analysisBuilder
                .build()
                .also {
                    it.setAnalyzer(cameraExecutor) { imageProxy ->
                        processFrame(imageProxy)
                    }
                }

            try {
                cameraProvider.unbindAll()
                cameraProvider.bindToLifecycle(this, cameraSelector, imageAnalysis)
            } catch (exc: Exception) {
                Log.e(TAG, "Use case binding failed", exc)
            }
        }, ContextCompat.getMainExecutor(this))
    }


    @OptIn(ExperimentalCamera2Interop::class)
    private fun applySmoothFpsRange(
        builder: ImageAnalysis.Builder,
        cameraProvider: ProcessCameraProvider,
        cameraSelector: CameraSelector
    ) {
        try {
            val cameraInfo = cameraSelector.filter(cameraProvider.availableCameraInfos).firstOrNull()
                ?: return
            val ranges = Camera2CameraInfo.from(cameraInfo).getCameraCharacteristic(
                CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES
            ) ?: return

            val best = ranges
                .filter { it.upper == TARGET_FPS && it.lower >= MIN_SMOOTH_FPS }
                .minByOrNull { it.lower }
                ?: return

            Camera2Interop.Extender(builder).setCaptureRequestOption(
                CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, best
            )
            Log.d(TAG, "Requested AE fps range $best")
        } catch (e: Exception) {
            Log.w(TAG, "Could not set AE fps range; using camera default", e)
        }
    }

    private fun processFrame(imageProxy: ImageProxy) {
        val timestampNs = imageProxy.imageInfo.timestamp
        val elapsedNs = timestampNs - lastFrameTimestampNs
        if (lastFrameTimestampNs != 0L && elapsedNs >= 0 && elapsedNs < minFrameIntervalNs) {
            imageProxy.close()
            return
        }
        lastFrameTimestampNs = timestampNs

        try {
            val frame = ImageUtils.imageProxyToJpeg(
                imageProxy,
                quality = JPEG_QUALITY,
                additionalRotation = manualRotationOffset
            )
            frameRepository.updateFrame(frame)
            if (_previewFrame.subscriptionCount.value > 0) {
                _previewFrame.value = frame
            }
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
        _previewFrame.value = null
        cameraExecutor.shutdown()
        releaseLocks()
        Log.d(TAG, "Service destroyed")
    }
}
