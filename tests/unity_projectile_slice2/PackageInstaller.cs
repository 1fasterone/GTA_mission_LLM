using System.Linq;
using UnityEditor;
using UnityEditor.PackageManager;
using UnityEditor.PackageManager.Requests;
using UnityEngine;

namespace ProjectBootstrap
{
    public static class PackageInstaller
    {
        private const double TimeoutSeconds = 600d;
        private static readonly string[] PackagesToAdd =
        {
            "com.unity.test-framework",
        };
        private static readonly string[] PackagesToRemove = { };

        private static AddAndRemoveRequest _request;
        private static double _deadline;

        public static void Install()
        {
            Debug.Log("[PackageInstaller] Adding: " + string.Join(", ", PackagesToAdd));
            _request = Client.AddAndRemove(PackagesToAdd, PackagesToRemove);
            _deadline = EditorApplication.timeSinceStartup + TimeoutSeconds;
            EditorApplication.update += Poll;
        }

        private static void Poll()
        {
            if (_request == null) return;
            if (!_request.IsCompleted)
            {
                if (EditorApplication.timeSinceStartup <= _deadline) return;
                EditorApplication.update -= Poll;
                Debug.LogError("[PackageInstaller] Timed out waiting for UPM.");
                EditorApplication.Exit(2);
                return;
            }

            EditorApplication.update -= Poll;
            if (_request.Status == StatusCode.Success)
            {
                string resolved = string.Join(
                    ", ",
                    _request.Result.Select(package => package.name + "@" + package.version));
                Debug.Log("[PackageInstaller] Resolved: " + resolved);
                EditorApplication.Exit(0);
                return;
            }

            Debug.LogError("[PackageInstaller] Failed: " + _request.Error?.message);
            EditorApplication.Exit(1);
        }
    }
}
