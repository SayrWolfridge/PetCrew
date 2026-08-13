!macro NSIS_HOOK_PREINSTALL
  ; An upgrade cannot replace a running Core executable. Ending the exact
  ; package-owned task is harmless on a first install and leaves registration
  ; to the freshly installed Core below.
  nsExec::ExecToLog 'schtasks.exe /End /TN "PetCrew Core"'
  Pop $0
!macroend

!macro NSIS_HOOK_POSTINSTALL
  nsExec::ExecToStack '"$INSTDIR\petcrew-core.exe" --install-autostart'
  Pop $0
  Pop $1
  StrCmp $0 "0" petcrew_core_registered
    MessageBox MB_ICONEXCLAMATION|MB_OK "PetCrew установлен, но Core не удалось добавить в автозапуск. Запустите установщик повторно или обратитесь к инструкции по откату."
    Goto petcrew_core_install_done
  petcrew_core_registered:
  petcrew_core_install_done:
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  nsExec::ExecToStack '"$INSTDIR\petcrew-core.exe" --uninstall-autostart'
  Pop $0
  Pop $1
  StrCmp $0 "0" petcrew_core_removed
    MessageBox MB_ICONSTOP|MB_OK "Не удалось остановить и удалить задачу PetCrew Core. Удаление остановлено, чтобы не оставить работающий Core без файлов."
    Abort
  petcrew_core_removed:
!macroend
