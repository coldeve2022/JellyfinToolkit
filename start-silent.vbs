' Jellyfin Toolkit - portable silent launcher (no console window)
'
' This file is pure ASCII on purpose: VBScript files with non-ASCII
' characters break depending on the system code page.
'
' Resolution order:
'   1) JellyfinToolkit.exe next to this script (packaged release)
'   2) pythonw.exe found via the launcher / PATH (source checkout)
'   3) show a message box telling the user how to install Python

Option Explicit

Dim fso, shell, base, target, pythonw, cmd, exePath

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = base

exePath = base & "\JellyfinToolkit.exe"
If fso.FileExists(exePath) Then
    shell.Run """" & exePath & """", 1, False
    WScript.Quit 0
End If

' Source checkout: locate pythonw.exe without hardcoding any absolute path.
pythonw = ""
On Error Resume Next
pythonw = FindPythonW(base)
On Error GoTo 0

If pythonw = "" Then
    MsgBox "Cannot find pythonw.exe." & vbCrLf & vbCrLf & _
           "Install Python 3.11+ and tick ""Add python.exe to PATH""," & vbCrLf & _
           "then run:  pip install -r requirements.txt" & vbCrLf & vbCrLf & _
           "Or use the packaged JellyfinToolkit.exe.", _
           vbExclamation, "Jellyfin Toolkit"
    WScript.Quit 1
End If

cmd = """" & pythonw & """ """ & base & "\main.py"""
shell.Run cmd, 0, False

' ---------------------------------------------------------------
Function FindPythonW(baseDir)
    Dim candidates, i, p, fso2, shell2
    Set fso2 = CreateObject("Scripting.FileSystemObject")
    Set shell2 = CreateObject("WScript.Shell")

    ' a) local virtual environment in the project folder
    candidates = Array( _
        baseDir & "\.venv\Scripts\pythonw.exe", _
        baseDir & "\venv\Scripts\pythonw.exe", _
        baseDir & "\build_venv\Scripts\pythonw.exe" _
    )
    For i = 0 To UBound(candidates)
        If fso2.FileExists(candidates(i)) Then
            FindPythonW = candidates(i)
            Exit Function
        End If
    Next

    ' b) the "py" launcher (ships with python.org installers)
    p = ShellPath(shell2, "pyw.exe")
    If p <> "" Then
        FindPythonW = p
        Exit Function
    End If

    ' c) pythonw.exe already on PATH
    p = ShellPath(shell2, "pythonw.exe")
    If p <> "" Then
        FindPythonW = p
        Exit Function
    End If

    FindPythonW = ""
End Function

' Resolve a bare executable name through PATH without using `where`
' (avoids spawning a console window). Tries the edition-specific
' WindowsApps / LocalAppData locations as a last resort.
Function ShellPath(sh, name)
    Dim p, guess, i
    p = ""
    On Error Resume Next
    p = sh.RegRead("HKCU\Software\Python\PyLauncher\InstallPath\ExecutablePath")
    If Err.Number <> 0 Then Err.Clear
    On Error GoTo 0
    If p <> "" Then
        guess = Mid(p, 1, InStrRev(p, "\")) & name
        If fso.FileExists(guess) Then
            ShellPath = guess
            Exit Function
        End If
    End If

    Dim dirs
    dirs = Array( _
        sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python"), _
        sh.ExpandEnvironmentStrings("%ProgramFiles%\Python"), _
        sh.ExpandEnvironmentStrings("%USERPROFILE%\AppData\Local\Microsoft\WindowsApps") _
    )
    Dim root, ver, sub1
    For i = 0 To UBound(dirs)
        root = dirs(i)
        If fso.FolderExists(root) Then
            If LCase(Right(root, 10)) = "windowsapps" Then
                guess = root & "\" & name
                If fso.FileExists(guess) Then
                    ShellPath = guess
                    Exit Function
                End If
            Else
                For Each sub1 In fso.GetFolder(root).SubFolders
                    guess = sub1.Path & "\" & name
                    If fso.FileExists(guess) Then
                        ShellPath = guess
                        Exit Function
                    End If
                Next
            End If
        End If
    Next

    ShellPath = ""
End Function
