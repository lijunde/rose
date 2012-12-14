program extract_compass_log
implicit none
character(31):: dt_hr_env
character(255) :: pos_fpath
double precision::heading, speed_kn=5.0, dt_hr
real::random_no, rand
double precision:: ang_distance, lat, long, new_lat, new_long
double precision, parameter:: pi=3.141592654, radius_earth_nm=3443.89
integer:: code, clock
logical::l_verbose=.false.
namelist /report_nl/ l_verbose


call get_environment_variable("POS_FPATH", value=pos_fpath, status=code)
if (code /= 0) then
    write(0, *) "$POS_FPATH: not set."
    stop 1
end if

open(1, file=pos_fpath, action="read", iostat=code)
if (code /= 0) then
    write(0, *), pos_fpath, ": position file read failed."
    stop 1
end if
read(1, *) lat, long
close(1)

lat = (pi/180.0)*lat
long = (pi/180.0)*long

open(1, file="report.nl", action='read', status='old', iostat=code)
if (code == 0) then
    read(1, nml=report_nl)
    close(1)
end if

call get_environment_variable("TIME_INTERVAL_HRS", value=dt_hr_env, status=code)
if (code /= 0) then
    write(0, *) "$TIME_INTERVAL_HRS: not set"
    stop 1
end if
read(dt_hr_env, *) dt_hr

!Pretend to extract an average heading from the ship's compass
call system_clock(count=clock)
random_no = rand(clock)
heading = random_no*2*pi

! This is how far we went, in radians:
! (1 knot = 1 nautical mile / 1 hour)
ang_distance = (speed_kn*dt_hr)/radius_earth_nm

new_lat = asin(sin(lat)*cos(ang_distance) + &
               cos(lat)*sin(ang_distance)*cos(heading))
new_long = long + &
           atan2(sin(heading)*sin(ang_distance)*cos(lat), &
                 cos(ang_distance)-sin(lat)*sin(new_lat))
new_lat = (180.0/pi)*new_lat
new_long = (180.0/pi)*new_long

if (l_verbose) then
    print*, "New position, me hearties:", new_lat, new_long
end if

open(1, file=pos_fpath, action='write')
write(1, *) new_lat, new_long
close(1)
end program
