## 사전 준비
### 코드 가져와서 build하기
```
source /opt/ros/jazzy/setup.bash
git clone -b dev --single-branch https://github.com/addinedu-roscamp-10th/roscamp-repo-2
cd ~/roscamp-reop-2/device/smartcast_amr
git submodule update --init --recursive
colcon build --symlink-install
source install/setup.bash
 ```

---

### AMR rpi 내부에서
```yaml
// tat/tat_bringup/config/robot_config.yaml
namespace: "TAT" -> 사용할 로봇의 이름 설정
```

---

### linux 24.04 laptop에서
#### cli에서
현재 맵 경로는 `src/tat/tat_navigation/map/Final_map.yaml`
```
ros2 launch tat_navigation bringup_launch.py \\
  namespace:=<실행 로봇 namespace> \\
  map:="<맵 경로/파일명.yaml>"
```
<br>

rviz 상에서 로봇 한 대 확인 <br>
```cli
ros2 launch tat_navigation nav2_view.launch.xml namespace:=<실행 로봇 namespace>
```
<br>

rviz 상에서 여러 로봇 확인<br>
```
ros2 launch tat_navigation multi_nav2_view.launch.xml
```
