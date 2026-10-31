using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

// A real Windows application gives Start its own executable, icon and identity.
internal static class WindowsLauncher
{
    [STAThread]
    private static int Main(string[] args)
    {
        bool checkOnly = args.Contains("--check");
        args = args.Where(arg => arg != "--check").ToArray();
        using (var mutex = new Mutex(false, "Local\\akubot.StartupLauncher"))
        {
            bool acquired;
            try { acquired = mutex.WaitOne(0); }
            catch (AbandonedMutexException) { acquired = true; }
            if (!acquired)
            {
                if (!checkOnly) MessageBox.Show("akubot is already starting. Check its startup window.", "akubot");
                return 0;
            }
            try
            {
                using (var log = new StreamWriter(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "startup.log"), false, Encoding.UTF8))
                {
                    log.AutoFlush = true;
                    if (checkOnly) return Run(args, true, line => log.WriteLine(line));
                    Application.EnableVisualStyles();
                    Application.SetCompatibleTextRenderingDefault(false);
                    using (var window = new Form())
                    using (var output = new TextBox())
                    {
                        window.Text = "akubot";
                        window.ClientSize = new Size(580, 270);
                        window.StartPosition = FormStartPosition.CenterScreen;
                        window.MinimumSize = new Size(450, 240);
                        window.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
                        output.Multiline = true;
                        output.ReadOnly = true;
                        output.Dock = DockStyle.Fill;
                        output.ScrollBars = ScrollBars.Vertical;
                        output.Font = new Font("Segoe UI", 10);
                        output.BackColor = Color.White;
                        window.Controls.Add(output);
                        Task<int> startup = null;
                        Action<string> report = line =>
                        {
                            log.WriteLine(line);
                            if (!window.IsDisposed && window.IsHandleCreated)
                            {
                                try { window.BeginInvoke((Action)(() => output.AppendText(line + Environment.NewLine))); }
                                catch (InvalidOperationException) { }
                            }
                        };
                        window.Shown += delegate
                        {
                            startup = Task.Run(() => Run(args, false, report));
                            startup.ContinueWith(completed =>
                            {
                                if (!window.IsDisposed && window.IsHandleCreated)
                                {
                                    try
                                    {
                                        window.BeginInvoke((Action)(() => {
                                            if (completed.Result == 0) window.Close();
                                            else window.Text = "akubot: startup needs attention";
                                        }));
                                    }
                                    catch (InvalidOperationException) { }
                                }
                            });
                        };
                        Application.Run(window);
                        // If the window is closed early, finish startup in the background
                        // before disposing the log or releasing the startup mutex.
                        if (startup != null) startup.GetAwaiter().GetResult();
                    }
                }
                return 0;
            }
            catch (Exception)
            {
                if (!checkOnly) MessageBox.Show("akubot could not open its startup window. Check the launcher folder permissions.", "akubot");
                return 1;
            }
            finally { mutex.ReleaseMutex(); }
        }
    }

    private static int Run(string[] args, bool checkOnly, Action<string> report)
    {
        try
        {
            string script = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "Start-akubot.ps1");
            if (!File.Exists(script)) { report("The akubot startup script is missing. Reinstall the shortcuts."); return 1; }
            string command = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Quote(script);
            foreach (string arg in args) command += " " + Quote(arg);
            if (checkOnly) command += " -CheckOnly";
            var info = new ProcessStartInfo {
                FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell\\v1.0\\powershell.exe"),
                Arguments = command,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            report(checkOnly ? "Checking akubot without starting any containers..." : "Starting akubot...");
            using (var process = Process.Start(info))
            {
                Task<string> errors = process.StandardError.ReadToEndAsync();
                string line;
                while ((line = process.StandardOutput.ReadLine()) != null) report(line);
                process.WaitForExit();
                string errorText = errors.GetAwaiter().GetResult();
                if (!String.IsNullOrWhiteSpace(errorText)) report(errorText.Trim());
                if (process.ExitCode != 0) report("You can close this window after reading the error. No settings or models were changed.");
                return process.ExitCode;
            }
        }
        catch (Exception) { report("Windows could not launch akubot. Check that WSL and PowerShell are available."); return 1; }
    }

    // Standard Windows command-line quoting, including trailing backslashes.
    private static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '\"') { result.Append('\\', slashes * 2 + 1); result.Append(c); }
            else { result.Append('\\', slashes); result.Append(c); }
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        result.Append('\"');
        return result.ToString();
    }
}
