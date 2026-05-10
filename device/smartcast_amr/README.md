## 사전 준비
### 코드 가져와서 build하기(amr과 laptop 공통)
#### cli(터미널)에서
```bash
cd ~
source /opt/ros/jazzy/setup.bash
git clone -b dev --single-branch https://github.com/addinedu-roscamp-10th/roscamp-repo-2 # dev 수정 예정
cd ~/roscamp-reop-2/device/smartcast_amr
git submodule update --init --recursive # amr에서만 실행
rosdep install --from-paths src --ignore-src -r -y
colcon build --symlink-install
source ~/roscamp-repo-2/device/smartcast_amr/install/setup.bash
 ```

---

### AMR rpi에서
#### cli(터미널)에서
```bash
ros2 launch tat_bringup bringup_robot.launch.py namespace:=<실행 로봇 namespace>
```
---

### linux 24.04 laptop에서
현재 기본 맵 경로는 `src/tat/tat_navigation/map/Final_map.yaml`
<br>
맵 변경 시 `src/tat/tat_navigation/launch/bringup_launch.py` 89번째 줄도 함께 변경해야함.
#### cli(터미널)에서

```bash
# 여러 로봇을 실행하려면 다른 터미널에서 namespace에 맞추어 로봇 개수만큼 실행
ros2 launch tat_navigation bringup_launch.py namespace:=<실행할 로봇의 namespace>
```

<br>

rviz 상에서 로봇 한 대 확인 <br>
```bash
ros2 launch tat_navigation nav2_view.launch.xml namespace:=<실행할 로봇의 namespace>
```
<br>

rviz 상에서 로봇 여러 대 확인<br>
```
ros2 launch tat_navigation multi_nav2_view.launch.xml
```
