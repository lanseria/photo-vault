//
//  BackgroundSupport.swift
//  PhotoVaultSync
//
//  AppDelegate：后台 URLSession 事件与 BGTaskScheduler 的接线点。
//  这两个钩子都必须在 didFinishLaunching 返回前准备好：
//    * 后台 URLSession 的 delegate 要先建出来，否则系统在冷启动时抛给 App 的
//      「后台传完了」事件会没人接收；
//    * BGTaskScheduler 的 launch handler 必须注册完，否则后台任务不会被唤醒。
//

import UIKit

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        // 两个都是单例副作用：创建后台会话 + 注册后台任务处理器
        _ = UploadClient.shared
        BackgroundSyncManager.shared.registerTasks()
        return true
    }

    /// 后台会话的事件全部收完后，系统通过这个回调交给 App，必须原样转给上传 client
    func application(
        _ application: UIApplication,
        handleEventsForBackgroundURLSession identifier: String,
        completionHandler: @escaping () -> Void
    ) {
        UploadClient.shared.handleBackgroundSessionEvents(identifier: identifier,
                                                          completionHandler: completionHandler)
    }
}
