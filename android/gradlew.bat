@echo off
setlocal
set DIRNAME=%~dp0
if defined JAVA_HOME goto useJavaHome
set JAVA_EXE=java.exe
goto execute
:useJavaHome
set JAVA_EXE=%JAVA_HOME%\bin\java.exe
:execute
"%JAVA_EXE%" -Xmx64m -Xms64m -Dorg.gradle.appname=gradlew -classpath "" -jar "%DIRNAME%gradle\wrapper\gradle-wrapper.jar" %*
endlocal

