//! Ownership of processes launched by the experimental supervisor.
//! Windows jobs close with the manager; Unix uses a dedicated process group.
use std::process::{Child, Command};

#[cfg(windows)]
pub struct ProcessTree(std::os::windows::io::OwnedHandle);
#[cfg(unix)]
pub struct ProcessTree;

impl ProcessTree {
    #[cfg(windows)]
    pub fn new() -> Result<Self, &'static str> {
        use std::os::windows::io::FromRawHandle;
        use windows_sys::Win32::System::JobObjects::{
            CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };
        // SAFETY: null attributes and name request a new unnamed Windows job.
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err("cannot create process job");
        }
        // SAFETY: CreateJobObjectW returned a unique valid owned handle.
        let owned = unsafe { std::os::windows::io::OwnedHandle::from_raw_handle(handle) };
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        // SAFETY: info is valid for the duration of the call and its exact size is provided.
        let ok = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                (&info as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                std::mem::size_of_val(&info) as u32,
            )
        };
        if ok == 0 {
            return Err("cannot configure process job");
        }
        Ok(Self(owned))
    }
    #[cfg(unix)]
    pub fn new() -> Result<Self, &'static str> {
        Ok(Self)
    }

    pub fn configure(&self, command: &mut Command) {
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
    }
    #[cfg(windows)]
    pub fn attach(&self, child: &Child) -> Result<(), &'static str> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::JobObjects::AssignProcessToJobObject;
        // SAFETY: both handles are live and owned by the caller throughout the call.
        if unsafe { AssignProcessToJobObject(self.0.as_raw_handle(), child.as_raw_handle()) } == 0 {
            return Err("cannot assign child to process job");
        }
        Ok(())
    }
    #[cfg(unix)]
    pub fn attach(&self, _child: &Child) -> Result<(), &'static str> {
        Ok(())
    }
    #[cfg(target_os = "linux")]
    pub fn poll(
        &self,
        child: &mut Child,
    ) -> Result<Option<std::process::ExitStatus>, &'static str> {
        let pid = child.id();
        // SAFETY: a zeroed siginfo is a valid output buffer for waitid.
        let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
        // WNOWAIT keeps the leader unreaped, preventing PID reuse while its group is cleaned up.
        // SAFETY: info points to writable memory and pid is the owned direct child.
        let rc = unsafe {
            libc::waitid(
                libc::P_PID,
                pid,
                &mut info,
                libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
            )
        };
        if rc != 0 {
            return Err("cannot inspect child process");
        }
        // SAFETY: waitid succeeded and initialized siginfo.
        if unsafe { info.si_pid() } == 0 {
            return Ok(None);
        }
        let group = i32::try_from(pid).map_err(|_| "invalid process id")?;
        if group <= 1 {
            return Err("invalid process id");
        }
        // SAFETY: the leader remains an unreaped child, so its process group ID cannot be reused.
        let rc = unsafe { libc::kill(-group, libc::SIGKILL) };
        if rc != 0 && std::io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH) {
            return Err("cannot terminate exited child's process group");
        }
        child.try_wait().map_err(|_| "cannot reap process")
    }
    #[cfg(not(target_os = "linux"))]
    pub fn poll(
        &self,
        child: &mut Child,
    ) -> Result<Option<std::process::ExitStatus>, &'static str> {
        child.try_wait().map_err(|_| "cannot inspect process")
    }
    #[cfg(windows)]
    pub fn terminate(&self, _child: &Child) -> Result<(), &'static str> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::JobObjects::TerminateJobObject;
        // SAFETY: the job handle remains owned and valid for this call.
        if unsafe { TerminateJobObject(self.0.as_raw_handle(), 1) } == 0 {
            return Err("cannot terminate process job");
        }
        Ok(())
    }
    #[cfg(unix)]
    pub fn terminate(&self, child: &Child) -> Result<(), &'static str> {
        let pid = i32::try_from(child.id()).map_err(|_| "invalid process id")?;
        if pid <= 1 {
            return Err("invalid process id");
        }
        // SAFETY: child was started in its own process group; it has not been reaped,
        // so its PID cannot have been reused. Negative PID addresses only that group.
        if unsafe { libc::kill(-pid, libc::SIGKILL) } != 0 {
            return Err("cannot terminate process group");
        }
        Ok(())
    }
}
