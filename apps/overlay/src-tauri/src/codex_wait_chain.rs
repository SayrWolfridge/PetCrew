//! Read-only Windows wait-chain probe. Never terminates a process.

use serde::{Deserialize, Serialize};
use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
use windows_sys::Win32::System::Diagnostics::Debug::{
    CloseThreadWaitChainSession, GetThreadWaitChain, OpenThreadWaitChainSession,
    WctProcessWaitType, WctThreadType, WAITCHAIN_NODE_INFO, WCT_MAX_NODE_COUNT,
    WCT_OUT_OF_PROC_FLAG,
};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct ProcessWait {
    pub owner_thread_id: u32,
    pub target_process_id: u32,
    pub wait_ms: u32,
    pub cycle: bool,
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct WaitChainProbeEnvelope {
    pub waits: Option<Vec<ProcessWait>>,
    pub error: Option<String>,
}

pub(crate) fn probe_envelope(process_id: u32) -> WaitChainProbeEnvelope {
    match probe_desktop_app_server(process_id) {
        Ok(waits) => WaitChainProbeEnvelope {
            waits: Some(waits),
            error: None,
        },
        Err(error) => WaitChainProbeEnvelope {
            waits: None,
            error: Some(
                match error.as_str() {
                    "Codex Desktop неоднозначен" => "desktop_ambiguous",
                    "PID не принадлежит единственному Desktop App Server" => {
                        "app_server_identity_changed"
                    }
                    "Windows не предоставила список потоков App Server" => {
                        "thread_snapshot_unavailable"
                    }
                    "Слишком много потоков App Server для ограниченной проверки" => {
                        "app_server_thread_limit_exceeded"
                    }
                    "Потоки App Server не найдены" => "app_server_threads_missing",
                    "Windows не открыла диагностику цепочек ожидания" => {
                        "wct_session_unavailable"
                    }
                    _ => "process_inventory_failed",
                }
                .into(),
            ),
        },
    }
}

pub(crate) fn probe_desktop_app_server(process_id: u32) -> Result<Vec<ProcessWait>, String> {
    let mains = crate::codex_desktop_control::main_processes()?;
    let [main_pid] = mains.as_slice() else {
        return Err("Codex Desktop неоднозначен".into());
    };
    if crate::codex_desktop_control::app_server_children(*main_pid)? != vec![process_id] {
        return Err("PID не принадлежит единственному Desktop App Server".into());
    }
    process_waits(process_id)
}

pub(crate) fn process_waits(process_id: u32) -> Result<Vec<ProcessWait>, String> {
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Err("Windows не предоставила список потоков App Server".into());
    }
    let mut entry = THREADENTRY32 {
        dwSize: std::mem::size_of::<THREADENTRY32>() as u32,
        ..Default::default()
    };
    let mut thread_ids = Vec::new();
    let mut found = unsafe { Thread32First(snapshot, &mut entry) } != 0;
    while found {
        if entry.th32OwnerProcessID == process_id {
            thread_ids.push(entry.th32ThreadID);
            if thread_ids.len() > 512 {
                unsafe { CloseHandle(snapshot) };
                return Err("Слишком много потоков App Server для ограниченной проверки".into());
            }
        }
        found = unsafe { Thread32Next(snapshot, &mut entry) } != 0;
    }
    unsafe { CloseHandle(snapshot) };
    if thread_ids.is_empty() {
        return Err("Потоки App Server не найдены".into());
    }

    let session = unsafe { OpenThreadWaitChainSession(0, None) };
    if session.is_null() {
        return Err("Windows не открыла диагностику цепочек ожидания".into());
    }
    let mut waits = Vec::new();
    for thread_id in thread_ids {
        let mut nodes = [WAITCHAIN_NODE_INFO::default(); WCT_MAX_NODE_COUNT as usize];
        let mut count = WCT_MAX_NODE_COUNT;
        let mut cycle = 0;
        let ok = unsafe {
            GetThreadWaitChain(
                session,
                0,
                WCT_OUT_OF_PROC_FLAG,
                thread_id,
                &mut count,
                nodes.as_mut_ptr(),
                &mut cycle,
            )
        };
        if ok == 0 || count < 3 || count > WCT_MAX_NODE_COUNT {
            continue;
        }
        for index in 1..(count as usize - 1) {
            if nodes[index].ObjectType != WctProcessWaitType
                || nodes[index + 1].ObjectType != WctThreadType
            {
                continue;
            }
            let thread = unsafe { nodes[index + 1].Anonymous.ThreadObject };
            if thread.ProcessId == 0 || thread.ProcessId == process_id {
                continue;
            }
            let first = unsafe { nodes[0].Anonymous.ThreadObject };
            waits.push(ProcessWait {
                owner_thread_id: thread_id,
                target_process_id: thread.ProcessId,
                wait_ms: first.WaitTime,
                cycle: cycle != 0,
            });
        }
    }
    unsafe { CloseThreadWaitChainSession(session) };
    Ok(waits)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[ignore = "manual read-only Windows wait-chain probe of one Desktop-owned App Server"]
    fn live_app_server_wait_chain_probe() {
        let mains = crate::codex_desktop_control::main_processes().unwrap();
        let [main_pid] = mains.as_slice() else {
            panic!("expected one installed Codex Desktop")
        };
        let children = crate::codex_desktop_control::app_server_children(*main_pid).unwrap();
        let [app_server_pid] = children.as_slice() else {
            panic!("expected one Desktop-owned App Server")
        };
        let waits = process_waits(*app_server_pid).unwrap();
        println!("app_server_pid={app_server_pid} process_waits={waits:?}");
    }
}
