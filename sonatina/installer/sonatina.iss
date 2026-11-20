; Sonatina installer (Inno Setup 6). Built by .github/workflows/host.yml:
;   ISCC.exe /DAppVersion=1.0.0 /DBuildDir=<repo>\build sonatina\installer\sonatina.iss
; BuildDir must contain: dist\Sonatina\ (PyInstaller onedir), firmware\, wdi\wdi-simple.exe

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef BuildDir
  #define BuildDir "..\..\build"
#endif

[Setup]
AppId={{6F1C2E7A-5B1D-4C55-9C2E-50A7A1A1C0DE}
AppName=Sonatina
AppVersion={#AppVersion}
AppVerName=Sonatina {#AppVersion}
AppPublisher=Sonatina
DefaultDirName={autopf}\Sonatina
DefaultGroupName=Sonatina
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
OutputDir={#BuildDir}\installer
OutputBaseFilename=SonatinaSetup-{#AppVersion}
SetupIconFile=sonatina.ico
UninstallDisplayIcon={app}\Sonatina.exe
UninstallDisplayName=Sonatina
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "it"; MessagesFile: "compiler:Languages\Italian.isl"

[CustomMessages]
en.AdapterTitle=Bluetooth adapter for Sonatina
en.AdapterDesc=Choose the USB Bluetooth adapter that Sonatina will use for your headphones.
en.AdapterSub=Windows will no longer use the chosen adapter: keep another one (or the built-in Bluetooth) for mice, keyboards and other devices. Plug the adapter in now if you do not see it, then press Back and Next.
en.AdapterNone=No USB Bluetooth adapter found. Plug one in, then press Back and Next.
en.AdapterWindows=used by Windows
en.AdapterWinusb=already dedicated to Sonatina
en.AdapterConfirm=Windows will stop using "%1" for its own Bluetooth devices. Continue?
en.AdapterPick=Choose an adapter.
en.VBTitle=Virtual audio output
en.VBDesc=Sonatina receives the sound of Windows through VB-CABLE (free, by VB-Audio).
en.VBInstall=Download and install VB-CABLE now (required; in its window click "Install Driver")
en.VBPresent=VB-CABLE is already installed.
en.StatusAdapter=Giving the adapter to Sonatina...
en.StatusVB=Installing VB-CABLE...
en.AdapterFailed=The adapter could not be configured (code %1). Details: %2
en.VBFailed=VB-CABLE could not be downloaded: install it from https://vb-audio.com/Cable/ and restart Sonatina.
en.RunApp=Start Sonatina (it will look for headphones in pairing mode)
it.AdapterTitle=Chiavetta Bluetooth per Sonatina
it.AdapterDesc=Scegli la chiavetta Bluetooth USB che Sonatina usera' per le cuffie.
it.AdapterSub=Windows non usera' piu' la chiavetta scelta: tienine un'altra (o il Bluetooth integrato) per mouse, tastiere e altri dispositivi. Se non la vedi, collegala ora e premi Indietro e poi Avanti.
it.AdapterNone=Nessuna chiavetta Bluetooth USB trovata. Collegala, poi premi Indietro e Avanti.
it.AdapterWindows=usata da Windows
it.AdapterWinusb=gia' dedicata a Sonatina
it.AdapterConfirm=Windows smettera' di usare "%1" per i suoi dispositivi Bluetooth. Continuare?
it.AdapterPick=Scegli una chiavetta.
it.VBTitle=Uscita audio virtuale
it.VBDesc=Sonatina riceve l'audio di Windows tramite VB-CABLE (gratuito, di VB-Audio).
it.VBInstall=Scarica e installa VB-CABLE adesso (necessario; nella sua finestra clicca "Install Driver")
it.VBPresent=VB-CABLE e' gia' installato.
it.StatusAdapter=Assegno la chiavetta a Sonatina...
it.StatusVB=Installo VB-CABLE...
it.AdapterFailed=Non sono riuscito a configurare la chiavetta (codice %1). Dettagli: %2
it.VBFailed=Download di VB-CABLE non riuscito: installalo da https://vb-audio.com/Cable/ e riavvia Sonatina.
it.RunApp=Avvia Sonatina (cerchera' le cuffie in modalita' abbinamento)

[Files]
Source: "{#BuildDir}\dist\Sonatina\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#BuildDir}\dashboard\*"; DestDir: "{app}\dashboard"; Flags: recursesubdirs createallsubdirs ignoreversion skipifsourcedoesntexist
Source: "{#BuildDir}\firmware\*"; DestDir: "{app}\firmware"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#BuildDir}\wdi\wdi-simple.exe"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "sonatina-setup.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "sonatina-setup.ps1"; Flags: dontcopy

[InstallDelete]
Type: filesandordirs; Name: "{app}\dashboard"

[Icons]
Name: "{group}\Sonatina"; Filename: "{app}\Sonatina.exe"
Name: "{group}\{cm:UninstallProgram,Sonatina}"; Filename: "{uninstallexe}"
Name: "{userstartup}\Sonatina"; Filename: "{app}\Sonatina.exe"; Check: StartupWanted

[Registry]
Root: HKLM; Subkey: "Software\Sonatina"; ValueType: string; ValueName: "AdapterVid"; ValueData: "{code:GetVid}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\Sonatina"; ValueType: string; ValueName: "AdapterPid"; ValueData: "{code:GetPid}"; Flags: uninsdeletekey

[Run]
Filename: "{app}\Sonatina.exe"; Description: "{cm:RunApp}"; Flags: postinstall nowait runasoriginaluser skipifsilent

[Code]
var
  AdapterPage: TInputOptionWizardPage;
  VBPage: TInputOptionWizardPage;
  Vids, Pids, States, Names: TArrayOfString;
  VBMissing: Boolean;
  RestartNeeded: Boolean;

function PowerShell(const Args: String; var Code: Integer): Boolean;
begin
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\sonatina-setup.ps1') + '" ' + Args,
    '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function NextField(var S: String): String;
var
  P: Integer;
begin
  P := Pos('|', S);
  if P = 0 then begin
    Result := S;
    S := '';
  end else begin
    Result := Copy(S, 1, P - 1);
    S := Copy(S, P + 1, Length(S));
  end;
end;

procedure RefreshAdapters();
var
  Lines: TArrayOfString;
  I, N, Code, Selected: Integer;
  Line, Tag: String;
begin
  AdapterPage.CheckListBox.Items.Clear;
  SetArrayLength(Vids, 0); SetArrayLength(Pids, 0); SetArrayLength(States, 0); SetArrayLength(Names, 0);
  PowerShell('-ListAdapters -Out "' + ExpandConstant('{tmp}\adapters.txt') + '"', Code);
  if not LoadStringsFromFile(ExpandConstant('{tmp}\adapters.txt'), Lines) then
    SetArrayLength(Lines, 0);
  N := 0;
  Selected := -1;
  for I := 0 to GetArrayLength(Lines) - 1 do begin
    Line := Trim(Lines[I]);
    if Line = '' then continue;
    SetArrayLength(Vids, N + 1); SetArrayLength(Pids, N + 1);
    SetArrayLength(States, N + 1); SetArrayLength(Names, N + 1);
    Vids[N] := NextField(Line);
    Pids[N] := NextField(Line);
    States[N] := NextField(Line);
    Names[N] := Line;
    if States[N] = 'winusb' then begin
      Tag := CustomMessage('AdapterWinusb');
      Selected := N;
    end else
      Tag := CustomMessage('AdapterWindows');
    AdapterPage.Add(Names[N] + '  (' + Vids[N] + ':' + Pids[N] + ', ' + Tag + ')');
    N := N + 1;
  end;
  if N = 0 then
    AdapterPage.SubCaptionLabel.Caption := CustomMessage('AdapterNone')
  else begin
    AdapterPage.SubCaptionLabel.Caption := CustomMessage('AdapterSub');
    if Selected >= 0 then
      AdapterPage.SelectedValueIndex := Selected;
  end;
end;

procedure InitializeWizard();
var
  Code: Integer;
begin
  ExtractTemporaryFile('sonatina-setup.ps1');
  AdapterPage := CreateInputOptionPage(wpSelectDir, CustomMessage('AdapterTitle'),
    CustomMessage('AdapterDesc'), CustomMessage('AdapterSub'), True, True);
  VBMissing := True;
  if PowerShell('-HasVBCable', Code) and (Code = 0) then
    VBMissing := False;
  VBPage := CreateInputOptionPage(AdapterPage.ID, CustomMessage('VBTitle'), CustomMessage('VBDesc'),
    '', False, False);
  if VBMissing then begin
    VBPage.Add(CustomMessage('VBInstall'));
    VBPage.Values[0] := True;
  end else
    VBPage.SubCaptionLabel.Caption := CustomMessage('VBPresent');
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = AdapterPage.ID then
    RefreshAdapters();
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  I: Integer;
begin
  Result := True;
  if CurPageID = AdapterPage.ID then begin
    I := AdapterPage.SelectedValueIndex;
    if (I < 0) or (I >= GetArrayLength(Vids)) then begin
      MsgBox(CustomMessage('AdapterPick'), mbError, MB_OK);
      Result := False;
    end else if States[I] = 'windows' then
      Result := MsgBox(FmtMessage(CustomMessage('AdapterConfirm'), [Names[I]]),
                       mbConfirmation, MB_YESNO) = IDYES;
  end;
end;

function GetVid(Param: String): String;
begin
  Result := '';
  if (AdapterPage.SelectedValueIndex >= 0) and (AdapterPage.SelectedValueIndex < GetArrayLength(Vids)) then
    Result := Vids[AdapterPage.SelectedValueIndex];
end;

function GetPid(Param: String): String;
begin
  Result := '';
  if (AdapterPage.SelectedValueIndex >= 0) and (AdapterPage.SelectedValueIndex < GetArrayLength(Pids)) then
    Result := Pids[AdapterPage.SelectedValueIndex];
end;

{ Start with Windows: always on a first install; on an update only if the user kept it on
  (the Sonatina window and the tray remove Startup\Sonatina.lnk when it is turned off). }
function StartupWanted(): Boolean;
begin
  if RegKeyExists(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F1C2E7A-5B1D-4C55-9C2E-50A7A1A1C0DE}_is1') then
    Result := FileExists(ExpandConstant('{userstartup}\Sonatina.lnk'))
  else
    Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  PowerShell('-StopAll', Code);   { installed app or development build owning the adapter }
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  Log: String;
begin
  if CurStep <> ssPostInstall then
    exit;
  Log := ExpandConstant('{app}\install.log');
  WizardForm.StatusLabel.Caption := CustomMessage('StatusAdapter');
  PowerShell('-BindAdapter -Vid ' + GetVid('') + ' -ProductId ' + GetPid('') +
             ' -Wdi "' + ExpandConstant('{app}\tools\wdi-simple.exe') + '" -Log "' + Log + '"', Code);
  if Code <> 0 then begin
    MsgBox(FmtMessage(CustomMessage('AdapterFailed'), [IntToStr(Code), Log]), mbError, MB_OK);
  end;
  if VBMissing and VBPage.Values[0] then begin
    WizardForm.StatusLabel.Caption := CustomMessage('StatusVB');
    PowerShell('-InstallVBCable -Log "' + Log + '"', Code);
    if Code <> 0 then
      MsgBox(CustomMessage('VBFailed'), mbError, MB_OK)
    else
      RestartNeeded := True;
  end;
end;

function NeedRestart(): Boolean;
begin
  Result := RestartNeeded;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
  Helper: String;
begin
  if CurUninstallStep <> usUninstall then
    exit;
  Helper := ExpandConstant('{app}\tools\sonatina-setup.ps1');
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -ExecutionPolicy Bypass -File "' + Helper + '" -StopAll', '', SW_HIDE,
       ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -ExecutionPolicy Bypass -File "' + Helper + '" -RestoreAdapter -Log "' +
       ExpandConstant('{app}\uninstall.log') + '"', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;
