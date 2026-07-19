import Foundation
import Dispatch

final class Supervisor {
    private let lock = NSLock()
    private var children: [Process] = []
    private var shuttingDown = false

    private func log(_ message: String) {
        let formatter = ISO8601DateFormatter()
        FileHandle.standardError.write(Data("[\(formatter.string(from: Date()))] \(message)\n".utf8))
    }

    private func configuredEnvironment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let hermesRoot = "/Users/diatche/.hermes/hermes-agent"
        let venv = "\(hermesRoot)/venv"
        env["HERMES_HOME"] = "/Users/diatche/.hermes"
        env["VIRTUAL_ENV"] = venv
        let pathPrefix = [
            "\(venv)/bin",
            "\(hermesRoot)/node_modules/.bin",
            "/Users/diatche/.nvm/versions/node/v24.14.0/bin",
            "/Users/diatche/.local/bin",
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin"
        ].joined(separator: ":")
        env["PATH"] = pathPrefix + ":" + (env["PATH"] ?? "")
        return env
    }

    private func launch(_ executable: String, _ arguments: [String], name: String) -> Process {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.currentDirectoryURL = URL(fileURLWithPath: "/Users/diatche/.hermes")
        process.environment = configuredEnvironment()
        process.standardOutput = FileHandle.standardOutput
        process.standardError = FileHandle.standardError
        process.terminationHandler = { [weak self] proc in
            guard let self else { return }
            self.log("\(name) exited status=\(proc.terminationStatus)")
            self.lock.lock()
            let shouldExit = !self.shuttingDown
            self.lock.unlock()
            if shouldExit {
                self.terminateAll()
                exit(proc.terminationStatus == 0 ? 0 : proc.terminationStatus)
            }
        }
        do {
            try process.run()
            log("started \(name) pid=\(process.processIdentifier): \(executable) \(arguments.joined(separator: " "))")
        } catch {
            log("failed to start \(name): \(error)")
            terminateAll()
            exit(1)
        }
        lock.lock()
        children.append(process)
        lock.unlock()
        return process
    }

    func start() {
        let hermes = "/Users/diatche/.hermes/hermes-agent/venv/bin/hermes"
        _ = launch(hermes, ["gateway", "run", "--replace"], name: "gateway")
        _ = launch(hermes, ["dashboard", "--host", "0.0.0.0", "--port", "9119", "--no-open", "--skip-build"], name: "dashboard")
    }

    func terminateAll() {
        lock.lock()
        shuttingDown = true
        let snapshot = children
        lock.unlock()
        for child in snapshot where child.isRunning {
            child.terminate()
        }
    }

    func waitForever() {
        RunLoop.current.run()
    }
}

let supervisor = Supervisor()

signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let termSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
termSource.setEventHandler {
    supervisor.terminateAll()
    exit(0)
}
termSource.resume()
let intSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
intSource.setEventHandler {
    supervisor.terminateAll()
    exit(0)
}
intSource.resume()

supervisor.start()
supervisor.waitForever()
